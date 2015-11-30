"""
Implementation of a deep deterministic policy gradient RL learner.
Roughly follows algorithm described in Lillicrap et al. (2015).
"""

from functools import partial

import tensorflow as tf
from tensorflow.models.rnn import rnn, rnn_cell, seq2seq

from rlcomp.pointer_network import ptr_net_decoder
from rlcomp import util


def policy_model(inp, mdp, spec, name="policy", reuse=None,
                 track_scope=None):
  """
  Predict actions for the given input batch.

  Returns:
    actions: `batch_size * action_dim`
  """

  # TODO remove magic numbers
  with tf.variable_scope(name, reuse=reuse,
                         initializer=tf.truncated_normal_initializer(stddev=0.5)):
    return util.mlp(inp, mdp.state_dim, mdp.action_dim,
                    hidden=spec.policy_dims, track_scope=track_scope)


def noise_gaussian(inp, actions, stddev, name="noiser"):
  # Support list `actions` argument.
  if isinstance(actions, list):
    return [noise_gaussian(inp, actions_t, stddev, name=name)
            for actions_t in actions]

  noise = tf.random_normal(tf.shape(actions), 0, stddev)
  return actions + noise


def critic_model(inp, actions, mdp, spec, name="critic", reuse=None,
                 track_scope=None):
  """
  Predict the Q-value of the given state-action pairs.

  Returns:
    `batch_size` vector of Q-value predictions.
  """

  # TODO remove magic numbers
  with tf.variable_scope(name, reuse=reuse,
                         initializer=tf.truncated_normal_initializer(stddev=0.25)):
    output = util.mlp(tf.concat(1, [inp, actions]),
                      mdp.state_dim + mdp.action_dim, 1,
                      hidden=spec.critic_dims, bias_output=True,
                      track_scope=track_scope)

    return tf.squeeze(tf.tanh(output))


class DPG(object):

  def __init__(self, mdp, spec, inputs=None, q_targets=None, tau=None,
               noiser=None, name="dpg"):
    """
    Args:
      mdp:
      spec:
      inputs: Tensor of input values
      q_targets: Tensor of Q-value targets
    """

    if noiser is None:
      # TODO remove magic number
      noiser = partial(noise_gaussian, stddev=0.1)
    self.noiser = noiser

    # Hyperparameters
    self.mdp_spec = mdp
    self.spec = spec

    # Inputs
    self.inputs = inputs
    self.q_targets = q_targets
    self.tau = tau

    self.name = name

    with tf.variable_scope(self.name) as vs:
      self._vs = vs

      self._make_params()
      self._make_inputs()
      self._make_graph()
      self._make_objectives()
      self._make_updates()

  def _make_params(self):
    pass

  def _make_inputs(self):
    self.inputs = (self.inputs
                   or tf.placeholder(tf.float32, (None, self.mdp_spec.state_dim),
                                     name="inputs"))
    self.q_targets = (self.q_targets
                      or tf.placeholder(tf.float32, (None,), name="q_targets"))
    self.tau = self.tau or tf.placeholder(tf.float32, (1,), name="tau")

  def _make_graph(self):
    # Build main model: actor
    self.a_pred = policy_model(self.inputs, self.mdp_spec, self.spec,
                               name="policy")
    self.a_explore = self.noiser(self.inputs, self.a_pred)

    # Build main model: critic (on- and off-policy)
    self.critic_on = critic_model(self.inputs, self.a_pred, self.mdp_spec,
                                  self.spec, name="critic")
    self.critic_off = critic_model(self.inputs, self.a_explore, self.mdp_spec,
                                   self.spec, name="critic", reuse=True)

    # Build tracking models.
    self.a_pred_track = policy_model(self.inputs, self.mdp_spec, self.spec,
                                     track_scope="%s/policy" % self.name,
                                     name="policy_track")
    self.critic_on_track = critic_model(self.inputs, self.a_pred, self.mdp_spec,
                                        self.spec, name="critic_track",
                                        track_scope="%s/critic" % self.name)

  def _make_objectives(self):
    # TODO: Hacky, will cause clashes if multiple DPG instances.
    # Can't instantiate a VS cleanly either, because policy params might be
    # nested in unpredictable way by subclasses.
    policy_params = [var for var in tf.all_variables()
                     if "policy/" in var.name]
    critic_params = [var for var in tf.all_variables()
                     if "critic/" in var.name]
    self.policy_params = policy_params
    self.critic_params = critic_params

    # Policy objective: maximize on-policy critic activations
    self.policy_objective = -tf.reduce_mean(self.critic_on)

    # Critic objective: minimize MSE of off-policy Q-value predictions
    q_errors = tf.square(self.critic_off - self.q_targets)
    self.critic_objective = tf.reduce_mean(q_errors)

  def _make_updates(self):
    # Make tracking updates.
    policy_track_update = util.track_model_updates(
         "%s/policy" % self.name, "%s/policy_track" % self.name, self.tau)
    critic_track_update = util.track_model_updates(
        "%s/critic" % self.name, "%s/critic_track" % self.name, self.tau)
    self.track_update = tf.group(policy_track_update, critic_track_update)

    # SGD updates are left to client.


class RecurrentDPG(DPG):

  """
  Abstract DPG sequence model.

  This recurrent DPG is recurrent over the policy / decision process, but not
  the input. This is in accord with most "recurrent" RL policies. This DPG can
  be made effectively recurrent over input if the state of some input
  recurrence is provided as the MDP state representation.

  With some input representation `batch_size * input_dim`, this class computes
  a rollout using a recurrent deterministic policy $\pi(inp, h_{t-1})$, where
  $h_{t-1}$ is some hidden representation computed in the recurrence.

  Concrete subclasses must implement environment dynamics *within TF* using
  the method `_loop_function`. This method describes subsequent decoder inputs
  given a decoder output (i.e., a policy output).
  """

  def __init__(self, mdp, spec, input_dim, vocab_size, seq_length, **kwargs):
    self.input_dim = input_dim
    self.vocab_size = vocab_size
    self.seq_length = seq_length

    super(RecurrentDPG, self).__init__(mdp, spec, **kwargs)

  def _make_inputs(self):
    self.inputs = (self.inputs
                   or tf.placeholder(tf.float32, (None, self.input_dim),
                                     name="inputs"))
    self.q_targets = (self.q_targets
                      or tf.placeholder(tf.float32, (None,), name="q_targets"))
    self.tau = self.tau or tf.placeholder(tf.float32, (1,), name="tau")

    # HACK: Provide inputs for single steps in recurrence.
    self.decoder_state_ind = tf.placeholder(
        tf.float32, (None, self.spec.policy_dims[0]), name="dec_state_ind")
    self.decoder_action_ind = tf.placeholder(
        tf.float32, (None, self.mdp_spec.action_dim), name="dec_action_ind")

  class PolicyRNNCell(rnn_cell.RNNCell):

    """
    Simple MLP policy.

    Maps from decoder hidden state to continuous action space using a basic
    feedforward neural network.
    """

    def __init__(self, cell, dpg):
      self._cell = cell
      self._dpg = dpg

    @property
    def input_size(self):
      return self._cell.input_size

    @property
    def output_size(self):
      return self._dpg.mdp_spec.action_dim

    @property
    def state_size(self):
      return self._cell.state_size

    def __call__(self, inputs, state, scope=None):
      # Run the wrapped cell.
      output, res_state = self._cell(inputs, state)

      with tf.variable_scope(scope or type(self).__name__):
        actions = policy_model(output, self._dpg.mdp_spec, self._dpg.spec)

      return actions, res_state

  def _make_graph(self):
    decoder_cell = rnn_cell.GRUCell(self.spec.policy_dims[0])
    decoder_cell = self._policy_cell(decoder_cell)

    # Prepare dummy decoder inputs.
    batch_size = tf.shape(self.inputs)[0]
    input_shape = tf.pack([batch_size, self.input_dim])
    decoder_inputs = [tf.zeros(input_shape, dtype=tf.float32)
                      for _ in range(self.seq_length)]
    # Force-set second dimenson of dec_inputs
    for dec_inp in decoder_inputs:
      dec_inp.set_shape((None, self.input_dim))

    # Build decoder loop function which maps from decoder outputs / policy
    # actions to decoder inputs.
    loop_function = self._loop_function()

    # Build dummy initial state for decoder.
    init_state = tf.zeros(tf.pack([batch_size, decoder_cell.state_size]))
    init_state.set_shape((None, decoder_cell.state_size))

    self.a_pred, self.decoder_states = seq2seq.rnn_decoder(
        decoder_inputs, init_state, decoder_cell,
        loop_function=loop_function)
    # Drop init state.
    self.decoder_states = self.decoder_states[1:]

    self.a_explore = self.noiser(self.inputs, self.a_pred)

    # Build main model: critic (on- and off-policy)
    self.critic_on_seq = self._critic(self.decoder_states, self.a_pred)
    self.critic_off_seq = self._critic(self.decoder_states, self.a_explore,
                                   reuse=True)

    # Build helper for predicting Q-value in an isolated state (not part of a
    # larger recurrence)
    a_pred_ind, _ = decoder_cell(self.inputs, self.decoder_state_ind)
    a_explore_ind = self.noiser(self.inputs, a_pred_ind)
    self.critic_on = critic_model(self.decoder_state_ind,
                                  a_pred_ind,
                                  self.mdp_spec, self.spec,
                                  name="critic", reuse=True)
    self.critic_off = critic_model(self.decoder_state_ind,
                                   a_explore_ind, self.mdp_spec,
                                   self.spec, name="critic",
                                   reuse=True)

    # DEV: monitor average activations
    tf.scalar_summary("a_pred_ind.mean", tf.reduce_mean(a_pred_ind))
    tf.scalar_summary("critic_on(a_pred_ind).mean", tf.reduce_mean(self.critic_on))

  def _make_updates(self):
    # TODO support tracking model
    pass

  def _policy_cell(self, decoder_cell):
    """
    Build a policy RNN cell wrapper around the given decoder cell.

    Args:
      decoder_cell: An `RNNCell` instance which implements the hidden-layer
        recurrence of the decoder / policy

    Returns:
      An `RNNCell` instance which wraps `decoder_cell` and produces outputs in
      action-space.
    """
    # By default, use a simple MLP policy.
    return self.PolicyRNNCell(decoder_cell, self)

  def _loop_function(self):
    """
    Build a function which maps from decoder outputs to decoder inputs.

    Returns:
      A function which accepts two arguments `output_t, t`. `output_t` is a
      `batch_size * action_dim` tensor and `t` is an integer.
    """
    raise NotImplementedError("abstract method")

  def _critic(self, states_list, actions_list, reuse=None):
    scores = []
    for t, (states_t, actions_t) in enumerate(zip(states_list, actions_list)):
      reuse_t = (reuse or t > 0) or None
      scores.append(critic_model(states_t, actions_t, self.mdp_spec,
                                 self.spec, name="critic", reuse=reuse_t))

    return scores

  def harden_actions(self, action_list):
    """
    Harden the given sequence of soft actions such that they describe a
    concrete trajectory.

    Args:
      action_list: List of Numpy matrices of shape `batch_size * action_dim`
    """
    # TODO: eventually we'd like to run this within a TF graph when possible.
    # We can probably define hardening solely with TF
    raise NotImplementedError("abstract method")


class PointerNetDPG(DPG):

  """
  Sequence-to-sequence pointer network DPG implementation.

  This recurrent DPG encodes an input float sequence `x1...xT` into an encoder
  memory sequence `e1...eT`. Using a recurrent decoder, it computes hidden
  states `d1...dT`. Combining these decoder states with an attention scan over
  the encoder memory at each timestep, it produces an entire rollout `a1...aT`
  (sequence of continuous action representations). The action at timestep `ai`
  is used to compute an input to the decoder for the next timestep.

  A recurrent critic model is applied to the action representation at each
  timestep.
  """

  def __init__(self, mdp, spec, input_dim, seq_length, bn_actions=False,
               **kwargs):
    """
    Args:
      mdp:
      spec:
      input_dim: Dimension of input values provided to encoder (`self.inputs`)
      seq_length:
      bn_actions: If true, batch-normalize action outputs.
    """
    self.input_dim = input_dim
    self.seq_length = seq_length

    self.bn_actions = bn_actions

    assert mdp.state_dim == spec.policy_dims[0] * 2

    super(PointerNetDPG, self).__init__(mdp, spec, **kwargs)

  def _make_params(self):
    if self.bn_actions:
      with tf.variable_scope("bn"):
        shape = (self.mdp_spec.action_dim,)
        self.bn_beta = tf.Variable(tf.constant(0.0, shape=shape), name="beta")
        self.bn_gamma = tf.Variable(tf.constant(1.0, shape=shape),
                                    name="gamma")

        # Track avg values of the beta + gamma (scale + shift)
        tf.scalar_summary("bn_beta.mean", tf.reduce_mean(self.bn_beta))
        tf.scalar_summary("bn_gamma.mean", tf.reduce_mean(self.bn_gamma))

  def _make_inputs(self):
    if not self.inputs:
      self.inputs = [tf.placeholder(tf.float32, (None, self.input_dim))
                     for _ in range(self.seq_length)]
    self.tau = self.tau or tf.placeholder(tf.float32, (1,), name="tau")

  def _make_graph(self):
    # Encode sequence.
    # TODO: MultilayerRNN?
    encoder_cell = rnn_cell.GRUCell(self.spec.policy_dims[0])
    _, self.encoder_states = rnn.rnn(encoder_cell, self.inputs,
                                     dtype=tf.float32, scope="encoder")
    assert len(self.encoder_states) == self.seq_length # DEV

    # Reshape encoder states into an "attention states" tensor of shape
    # `batch_size * seq_length * policy_dim`.
    attn_states = tf.concat(1, [tf.expand_dims(state_t, 1)
                                for state_t in self.encoder_states])

    # Build a simple GRU-powered recurrent decoder cell.
    decoder_cell = rnn_cell.GRUCell(self.spec.policy_dims[0])

    # Prepare dummy encoder input. This will only be used on the first
    # timestep; in subsequent timesteps, the `loop_function` we provide
    # will be used to dynamically calculate new input values.
    batch_size = tf.shape(self.inputs[0])[0]
    dec_inp_shape = tf.pack([batch_size, decoder_cell.input_size])
    dec_inp_dummy = tf.zeros(dec_inp_shape, dtype=tf.float32)
    dec_inp_dummy.set_shape((None, decoder_cell.input_size))
    dec_inp = [dec_inp_dummy] * self.seq_length

    # Build pointer-network decoder.
    self.a_pred, dec_states, dec_inputs = ptr_net_decoder(
        dec_inp, self.encoder_states[-1], attn_states, decoder_cell,
        loop_function=self._loop_function(), scope="decoder")
    # Store dynamically calculated inputs -- critic may want to use these
    self.decoder_inputs = dec_inputs
    # Again strip the initial state.
    self.decoder_states = dec_states[1:]

    # Optional batch normalization.
    if self.bn_actions:
      # Compute moments over all timesteps (treat as one big batch).
      batch_pred = tf.concat(0, self.a_pred)
      mean = tf.reduce_mean(batch_pred, 0)
      variance = tf.reduce_mean(tf.square(batch_pred - mean), 0)

      # TODO track running mean, avg with exponential averaging
      # in order to prepare test-time normalization value

      # Resize to make BN op happy. (It is built for 4-dim CV applications.)
      batch_pred = tf.expand_dims(tf.expand_dims(batch_pred, 1), 1)
      batch_pred = tf.nn.batch_norm_with_global_normalization(
          batch_pred, mean, variance, self.bn_beta, self.bn_gamma,
          0.001, True)
      self.a_pred = tf.split(0, self.seq_length, tf.squeeze(batch_pred))

    # Use noiser to build exploratory rollouts.
    self.a_explore = self.noiser(self.inputs, self.a_pred)

    # Build main model: recurrently apply a critic over the entire rollout.
    self.critic_on = self._critic(self.a_pred)
    self.critic_off = self._critic(self.a_explore, reuse=True)

    self._make_q_targets()

  def _make_q_targets(self):
    if not self.q_targets:
      self.q_targets = [tf.placeholder(tf.float32, (None,))
                        for _ in range(self.seq_length)]

  def _policy_params(self):
    return [var for var in tf.all_variables()
            if "encoder/" in var.name or "decoder/" in var.name]

  def _make_objectives(self):
    # TODO: Hacky, will cause clashes if multiple DPG instances.
    policy_params = self._policy_params()
    critic_params = [var for var in tf.all_variables()
                     if "critic/" in var.name]
    self.policy_params = policy_params
    self.critic_params = critic_params

    if self.bn_actions:
      bn_params = [self.bn_beta, self.bn_gamma]
      self.policy_params += bn_params
      self.critic_params += bn_params

    # Policy objective: maximize on-policy critic activations
    mean_critic_over_time = tf.add_n(self.critic_on) / self.seq_length
    mean_critic = tf.reduce_mean(mean_critic_over_time)
    self.policy_objective = -mean_critic

    # DEV
    tf.scalar_summary("critic(a_pred).mean", mean_critic)

    # Critic objective: minimize MSE of off-policy Q-value predictions
    q_errors = [tf.reduce_mean(tf.square(critic_off_t - q_targets_t))
                for critic_off_t, q_targets_t
                in zip(self.critic_off, self.q_targets)]
    self.critic_objective = tf.add_n(q_errors) / self.seq_length
    tf.scalar_summary("critic_objective", self.critic_objective)

    mean_critic_off = tf.reduce_mean(tf.add_n(self.critic_off)) / self.seq_length
    tf.scalar_summary("critic(a_explore).mean", mean_critic_off)

    tf.scalar_summary("a_pred.mean", tf.reduce_mean(tf.add_n(self.a_pred)) / self.seq_length)
    tf.scalar_summary("a_pred.maxabs", tf.reduce_max(tf.abs(tf.pack(self.a_pred))))

  def _make_updates(self):
    # TODO support tracking model
    pass

  def _loop_function(self):
    """
    Build a function which maps from decoder outputs to decoder inputs.

    Returns:
      A function which accepts two arguments `output_t, t`. `output_t` is a
      `batch_size * action_dim` tensor and `t` is an integer.
    """
    # Use logits from output layer to compute a weighted sum of encoder memory
    # elements.
    # TODO: Can we use encoder inputs instead here?
    attn_states = tf.concat(1, [tf.expand_dims(states_t, 1)
                                for states_t in self.encoder_states])
    def loop_fn(output_t, t):
      output_t = tf.nn.softmax(output_t)
      weighted_mems = attn_states * tf.expand_dims(output_t, 2)
      processed = tf.reduce_sum(weighted_mems, 1)
      return processed

    return loop_fn

  def _critic(self, actions_lst, reuse=None):
    scores = []

    # Here our state representation is a concatenation of 1) decoder hidden
    # state and 2) decoder input.
    states_lst = [tf.concat(1, [inputs_t, states_t])
                  for inputs_t, states_t
                  in zip(self.decoder_inputs, self.decoder_states)]

    # Evaluate Q(s, a) at each timestep.
    for t, (states_t, actions_t) in enumerate(zip(states_lst, actions_lst)):
      reuse_t = (reuse or t > 0) or None

      critic_input = tf.concat(1, [states_t, actions_t])
      scores.append(critic_model(states_t, actions_t, self.mdp_spec,
                                 self.spec, name="critic", reuse=reuse_t))

    return scores

  def harden_actions(self, action_list):
    """
    Harden the given sequence of soft actions such that they describe a
    concrete trajectory.

    Args:
      action_list: List of Numpy matrices of shape `batch_size * action_dim`
    """
    # TODO: eventually we'd like to run this within a TF graph when possible.
    # We can probably define hardening solely with TF
    raise NotImplementedError("abstract method")
