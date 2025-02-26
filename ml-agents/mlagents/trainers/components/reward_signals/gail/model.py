from typing import Tuple, List

import tensorflow as tf
from mlagents.trainers.models import LearningModel

EPSILON = 1e-7


class GAILModel(object):
    def __init__(
        self,
        policy_model: LearningModel,
        h_size: int = 128,
        learning_rate: float = 3e-4,
        encoding_size: int = 64,
        use_actions: bool = False,
        use_vail: bool = False,
        gradient_penalty_weight: float = 10.0,
    ):
        """
        The initializer for the GAIL reward generator.
        https://arxiv.org/abs/1606.03476
        :param policy_model: The policy of the learning algorithm
        :param h_size: Size of the hidden layer for the discriminator
        :param learning_rate: The learning Rate for the discriminator
        :param encoding_size: The encoding size for the encoder
        :param use_actions: Whether or not to use actions to discriminate
        :param use_vail: Whether or not to use a variational bottleneck for the
        discriminator. See https://arxiv.org/abs/1810.00821.
        """
        self.h_size = h_size
        self.z_size = 128
        self.alpha = 0.0005
        self.mutual_information = 0.5
        self.policy_model = policy_model
        self.encoding_size = encoding_size
        self.gradient_penalty_weight = gradient_penalty_weight
        self.use_vail = use_vail
        self.use_actions = use_actions  # True # Not using actions
        self.make_beta()
        self.make_inputs()
        self.create_network()
        self.create_loss(learning_rate)

    def make_beta(self) -> None:
        """
        Creates the beta parameter and its updater for GAIL
        """
        self.beta = tf.get_variable(
            "gail_beta",
            [],
            trainable=False,
            dtype=tf.float32,
            initializer=tf.ones_initializer(),
        )
        self.kl_div_input = tf.placeholder(shape=[], dtype=tf.float32)
        new_beta = tf.maximum(
            self.beta + self.alpha * (self.kl_div_input - self.mutual_information),
            EPSILON,
        )
        self.update_beta = tf.assign(self.beta, new_beta)

    def make_inputs(self) -> None:
        """
        Creates the input layers for the discriminator
        """
        self.done_expert = tf.placeholder(shape=[None, 1], dtype=tf.float32)
        self.done_policy = tf.placeholder(shape=[None, 1], dtype=tf.float32)

        if self.policy_model.brain.vector_action_space_type == "continuous":
            action_length = self.policy_model.act_size[0]
            self.action_in_expert = tf.placeholder(
                shape=[None, action_length], dtype=tf.float32
            )
            self.expert_action = tf.identity(self.action_in_expert)
        else:
            action_length = len(self.policy_model.act_size)
            self.action_in_expert = tf.placeholder(
                shape=[None, action_length], dtype=tf.int32
            )
            self.expert_action = tf.concat(
                [
                    tf.one_hot(self.action_in_expert[:, i], act_size)
                    for i, act_size in enumerate(self.policy_model.act_size)
                ],
                axis=1,
            )

        encoded_policy_list = []
        encoded_expert_list = []

        if self.policy_model.vec_obs_size > 0:
            self.obs_in_expert = tf.placeholder(
                shape=[None, self.policy_model.vec_obs_size], dtype=tf.float32
            )
            if self.policy_model.normalize:
                encoded_expert_list.append(
                    self.policy_model.normalize_vector_obs(self.obs_in_expert)
                )
                encoded_policy_list.append(
                    self.policy_model.normalize_vector_obs(self.policy_model.vector_in)
                )
            else:
                encoded_expert_list.append(self.obs_in_expert)
                encoded_policy_list.append(self.policy_model.vector_in)

        if self.policy_model.vis_obs_size > 0:
            self.expert_visual_in: List[tf.Tensor] = []
            visual_policy_encoders = []
            visual_expert_encoders = []
            for i in range(self.policy_model.vis_obs_size):
                # Create input ops for next (t+1) visual observations.
                visual_input = self.policy_model.create_visual_input(
                    self.policy_model.brain.camera_resolutions[i],
                    name="gail_visual_observation_" + str(i),
                )
                self.expert_visual_in.append(visual_input)

                encoded_policy_visual = self.policy_model.create_visual_observation_encoder(
                    self.policy_model.visual_in[i],
                    self.encoding_size,
                    LearningModel.swish,
                    1,
                    "gail_stream_{}_visual_obs_encoder".format(i),
                    False,
                )

                encoded_expert_visual = self.policy_model.create_visual_observation_encoder(
                    self.expert_visual_in[i],
                    self.encoding_size,
                    LearningModel.swish,
                    1,
                    "gail_stream_{}_visual_obs_encoder".format(i),
                    True,
                )
                visual_policy_encoders.append(encoded_policy_visual)
                visual_expert_encoders.append(encoded_expert_visual)
            hidden_policy_visual = tf.concat(visual_policy_encoders, axis=1)
            hidden_expert_visual = tf.concat(visual_expert_encoders, axis=1)
            encoded_policy_list.append(hidden_policy_visual)
            encoded_expert_list.append(hidden_expert_visual)

        self.encoded_expert = tf.concat(encoded_expert_list, axis=1)
        self.encoded_policy = tf.concat(encoded_policy_list, axis=1)

    def create_encoder(
        self, state_in: tf.Tensor, action_in: tf.Tensor, done_in: tf.Tensor, reuse: bool
    ) -> Tuple[tf.Tensor, tf.Tensor, tf.Tensor]:
        """
        Creates the encoder for the discriminator
        :param state_in: The encoded observation input
        :param action_in: The action input
        :param done_in: The done flags input
        :param reuse: If true, the weights will be shared with the previous encoder created
        """
        with tf.variable_scope("GAIL_model"):
            if self.use_actions:
                concat_input = tf.concat([state_in, action_in, done_in], axis=1)
            else:
                concat_input = state_in

            hidden_1 = tf.layers.dense(
                concat_input,
                self.h_size,
                activation=LearningModel.swish,
                name="gail_d_hidden_1",
                reuse=reuse,
            )

            hidden_2 = tf.layers.dense(
                hidden_1,
                self.h_size,
                activation=LearningModel.swish,
                name="gail_d_hidden_2",
                reuse=reuse,
            )

            z_mean = None
            if self.use_vail:
                # Latent representation
                z_mean = tf.layers.dense(
                    hidden_2,
                    self.z_size,
                    reuse=reuse,
                    name="gail_z_mean",
                    kernel_initializer=LearningModel.scaled_init(0.01),
                )

                self.noise = tf.random_normal(tf.shape(z_mean), dtype=tf.float32)

                # Sampled latent code
                self.z = z_mean + self.z_sigma * self.noise * self.use_noise
                estimate_input = self.z
            else:
                estimate_input = hidden_2

            estimate = tf.layers.dense(
                estimate_input,
                1,
                activation=tf.nn.sigmoid,
                name="gail_d_estimate",
                reuse=reuse,
            )
            return estimate, z_mean, concat_input

    def create_network(self) -> None:
        """
        Helper for creating the intrinsic reward nodes
        """
        if self.use_vail:
            self.z_sigma = tf.get_variable(
                "gail_sigma_vail",
                self.z_size,
                dtype=tf.float32,
                initializer=tf.ones_initializer(),
            )
            self.z_sigma_sq = self.z_sigma * self.z_sigma
            self.z_log_sigma_sq = tf.log(self.z_sigma_sq + EPSILON)
            self.use_noise = tf.placeholder(
                shape=[1], dtype=tf.float32, name="gail_NoiseLevel"
            )
        self.expert_estimate, self.z_mean_expert, _ = self.create_encoder(
            self.encoded_expert, self.expert_action, self.done_expert, reuse=False
        )
        self.policy_estimate, self.z_mean_policy, _ = self.create_encoder(
            self.encoded_policy,
            self.policy_model.selected_actions,
            self.done_policy,
            reuse=True,
        )
        self.discriminator_score = tf.reshape(
            self.policy_estimate, [-1], name="gail_reward"
        )
        self.intrinsic_reward = -tf.log(1.0 - self.discriminator_score + EPSILON)

    def create_gradient_magnitude(self) -> tf.Tensor:
        """
        Gradient penalty from https://arxiv.org/pdf/1704.00028. Adds stability esp.
        for off-policy. Compute gradients w.r.t randomly interpolated input.
        """
        expert = [self.encoded_expert, self.expert_action, self.done_expert]
        policy = [
            self.encoded_policy,
            self.policy_model.selected_actions,
            self.done_policy,
        ]
        interp = []
        for _expert_in, _policy_in in zip(expert, policy):
            alpha = tf.random_uniform(tf.shape(_expert_in))
            interp.append(alpha * _expert_in + (1 - alpha) * _policy_in)

        grad_estimate, _, grad_input = self.create_encoder(
            interp[0], interp[1], interp[2], reuse=True
        )

        grad = tf.gradients(grad_estimate, [grad_input])[0]

        # Norm's gradient could be NaN at 0. Use our own safe_norm
        safe_norm = tf.sqrt(tf.reduce_sum(grad ** 2, axis=-1) + EPSILON)
        gradient_mag = tf.reduce_mean(tf.pow(safe_norm - 1, 2))

        return gradient_mag

    def create_loss(self, learning_rate: float) -> None:
        """
        Creates the loss and update nodes for the GAIL reward generator
        :param learning_rate: The learning rate for the optimizer
        """
        self.mean_expert_estimate = tf.reduce_mean(self.expert_estimate)
        self.mean_policy_estimate = tf.reduce_mean(self.policy_estimate)

        self.discriminator_loss = -tf.reduce_mean(
            tf.log(self.expert_estimate + EPSILON)
            + tf.log(1.0 - self.policy_estimate + EPSILON)
        )

        if self.use_vail:
            # KL divergence loss (encourage latent representation to be normal)
            self.kl_loss = tf.reduce_mean(
                -tf.reduce_sum(
                    1
                    + self.z_log_sigma_sq
                    - 0.5 * tf.square(self.z_mean_expert)
                    - 0.5 * tf.square(self.z_mean_policy)
                    - tf.exp(self.z_log_sigma_sq),
                    1,
                )
            )
            self.loss = (
                self.beta * (self.kl_loss - self.mutual_information)
                + self.discriminator_loss
            )
        else:
            self.loss = self.discriminator_loss

        if self.gradient_penalty_weight > 0.0:
            self.loss += self.gradient_penalty_weight * self.create_gradient_magnitude()

        optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate)
        self.update_batch = optimizer.minimize(self.loss)
