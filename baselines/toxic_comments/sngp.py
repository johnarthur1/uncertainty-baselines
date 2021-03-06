# coding=utf-8
# Copyright 2020 The Uncertainty Baselines Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Bidirectional encoder representations from transformers (BERT) with SNGP.

Spectral-normalized neural Gaussian process (SNGP) [1] is a simple method to
improve a deterministic neural network's uncertainty. It simply applies spectral
normalization to the hidden layers, and then replace the dense output layer
with a Gaussian process layer.

## References:

[1]: Jeremiah Liu et al. Simple and Principled Uncertainty Estimation with
     Deterministic Deep Learning via Distance Awareness.
     _arXiv preprint arXiv:2006.10108_, 2020.
     https://arxiv.org/abs/2006.10108
[2]: Zhiyun Lu, Eugene Ie, Fei Sha. Uncertainty Estimation with Infinitesimal
     Jackknife.  _arXiv preprint arXiv:2006.07584_, 2020.
     https://arxiv.org/abs/2006.07584
"""

import os
import time
from absl import app
from absl import flags
from absl import logging

import edward2 as ed
import numpy as np
import tensorflow as tf

import uncertainty_baselines as ub
import bert_utils  # local file import
from uncertainty_baselines.datasets import base
from uncertainty_baselines.datasets import toxic_comments as ds
import uncertainty_metrics as um


# Data flags
flags.DEFINE_string(
    'in_dataset_dir', None,
    'Path to in-domain dataset (WikipediaToxicityDataset).')
flags.DEFINE_string(
    'ood_dataset_dir', None,
    'Path to out-of-domain dataset (CivilCommentsDataset).')
flags.DEFINE_string(
    'identity_dataset_dir', None,
    'Path to out-of-domain dataset with identity labels '
    '(CivilCommentsIdentitiesDataset).')

# Checkpoint flags
flags.DEFINE_string(
    'bert_dir', None,
    'Directory to BERT pre-trained checkpoints and config files.')
flags.DEFINE_string(
    'bert_ckpt_dir', None, 'Directory to BERT pre-trained checkpoints. '
    'If None then then default to {bert_dir}/bert_model.ckpt.')
flags.DEFINE_string(
    'bert_config_dir', None, 'Directory to BERT config files. '
    'If None then then default to {bert_dir}/bert_config.json.')

# Normalization flags.
flags.DEFINE_bool(
    'use_layer_norm_att', True,
    'Whether to apply layer normalization to the self-attention layers.')
flags.DEFINE_bool(
    'use_layer_norm_ffn', True,
    'Whether to apply layer normalization to the feedforward layers.')
flags.DEFINE_bool(
    'use_spec_norm_att', False,
    'Whether to apply spectral normalization to the self-attention layers.')
flags.DEFINE_bool(
    'use_spec_norm_ffn', False,
    'Whether to apply spectral normalization to the feedforward layers.')
flags.DEFINE_bool(
    'use_spec_norm_plr', True,
    'Whether to apply spectral normalization to the final CLS pooler layer.')
flags.DEFINE_integer(
    'spec_norm_iteration', 1,
    'Number of power iterations to perform for estimating '
    'the spectral norm of weight matrices.')
flags.DEFINE_float('spec_norm_bound', .95,
                   'Upper bound to spectral norm of weight matrices.')

# Gaussian process flags.
flags.DEFINE_bool('use_gp_layer', True,
                  'Whether to use Gaussian process as the output layer.')
flags.DEFINE_float('gp_bias', 0., 'The bias term for GP layer.')
flags.DEFINE_float(
    'gp_scale', 2.,
    'The length-scale parameter for the RBF kernel of the GP layer.')
flags.DEFINE_integer(
    'gp_hidden_dim', 768,
    'The hidden dimension of the GP layer, which corresponds to the number of '
    'random features used for the approximation.')
flags.DEFINE_bool(
    'gp_input_normalization', True,
    'Whether to normalize the input using LayerNorm for GP layer.'
    'This is similar to automatic relevance determination (ARD) in the classic '
    'GP learning.')
flags.DEFINE_float('gp_cov_ridge_penalty', 1e-3,
                   'Ridge penalty parameter for GP posterior covariance.')
flags.DEFINE_float(
    'gp_cov_discount_factor', 0.999,
    'The discount factor to compute the moving average of precision matrix.')
flags.DEFINE_float(
    'gp_mean_field_factor', 1e-4,
    'The tunable multiplicative factor used in the mean-field approximation '
    'for the posterior mean of softmax Gaussian process. If -1 then use '
    'posterior mode instead of posterior mean. See [2] for detail.')


# Optimization and evaluation flags
flags.DEFINE_integer('seed', 42, 'Random seed.')
flags.DEFINE_integer('per_core_batch_size', 32, 'Batch size per TPU core/GPU.')
flags.DEFINE_float(
    'base_learning_rate', 5e-5,
    'Base learning rate when total batch size is 128. It is '
    'scaled by the ratio of the total batch size to 128.')
flags.DEFINE_integer(
    'checkpoint_interval', 3,
    'Number of epochs between saving checkpoints. Use -1 to '
    'never save checkpoints.')
flags.DEFINE_integer('evaluation_interval', 1,
                     'Number of epochs between evaluation.')
flags.DEFINE_integer('num_bins', 15, 'Number of bins for ECE.')
flags.DEFINE_list(
    'fractions', ['0.0', '0.01', '0.05', '0.1', '0.15', '0.2'],
    'A list of fractions of total examples to send to '
    'the moderators (up to 1).')
flags.DEFINE_string('output_dir', '/tmp/toxic_comments', 'Output directory.')
flags.DEFINE_integer('train_epochs', 3, 'Number of training epochs.')
flags.DEFINE_float(
    'warmup_proportion', 0.1,
    'Proportion of training to perform linear learning rate warmup for. '
    'E.g., 0.1 = 10% of training.')
flags.DEFINE_float(
    'ece_label_threshold', 0.7,
    'Threshold used to convert toxicity score into binary labels for computing '
    'Expected Calibration Error (ECE). Default is 0.7 which is the threshold '
    'value recommended by Jigsaw team.')
flags.DEFINE_integer(
    'num_mc_samples', 1,
    'Number of Monte Carlo forward passes to collect for ensemble prediction. '
    'Currently can only be 1 since the model is deterministic.')

# Loss type
flags.DEFINE_enum('loss_type', 'cross_entropy', ['cross_entropy', 'mse', 'mae'],
                  'Type of loss function to use.')

# Accelerator flags.
flags.DEFINE_bool('use_gpu', False, 'Whether to run on GPU or otherwise TPU.')
flags.DEFINE_bool('use_bfloat16', False, 'Whether to use mixed precision.')
flags.DEFINE_integer('num_cores', 8, 'Number of TPU cores or number of GPUs.')
flags.DEFINE_string('tpu', None,
                    'Name of the TPU. Only used if use_gpu is False.')

# Prediction mode.
flags.DEFINE_bool('prediction_mode', False, 'Whether to predict only.')
flags.DEFINE_string('eval_checkpoint_dir', None,
                    'The directory to restore the model weights from for '
                    'prediction mode.')

FLAGS = flags.FLAGS


_MAX_SEQ_LENGTH = 512
_IDENTITY_LABELS = ('male', 'female', 'transgender', 'other_gender',
                    'heterosexual', 'homosexual_gay_or_lesbian', 'bisexual',
                    'other_sexual_orientation', 'christian', 'jewish', 'muslim',
                    'hindu', 'buddhist', 'atheist', 'other_religion', 'black',
                    'white', 'asian', 'latino', 'other_race_or_ethnicity',
                    'physical_disability',
                    'intellectual_or_learning_disability',
                    'psychiatric_or_mental_illness', 'other_disability')


@flags.multi_flags_validator(
    ['prediction_mode', 'eval_checkpoint_dir'],
    message='`eval_checkpoint_dir` should be provided in prediction mode')
def _check_checkpoint_dir_for_prediction_mode(flags_dict):
  return  not flags_dict['prediction_mode'] or (
      flags_dict['eval_checkpoint_dir'] is not None)


def save_prediction(data, path):
  with (tf.io.gfile.GFile(path + '.npy', 'w')) as test_file:
    np.save(test_file, np.array(data))


def resolve_bert_ckpt_and_config_dir(bert_dir, bert_config_dir, bert_ckpt_dir):
  """Resolves BERT checkpoint and config file directories."""

  missing_ckpt_or_config_dir = not (bert_ckpt_dir and bert_config_dir)
  if missing_ckpt_or_config_dir:
    if not bert_dir:
      raise ValueError('bert_dir cannot be empty.')

    if not bert_config_dir:
      bert_config_dir = os.path.join(bert_dir, 'bert_config.json')

    if not bert_ckpt_dir:
      bert_ckpt_dir = os.path.join(bert_dir, 'bert_model.ckpt')
  return bert_config_dir, bert_ckpt_dir


def create_feature_and_label(inputs):
  """Creates features and labels from model inputs."""
  input_ids = inputs['input_ids']
  input_mask = inputs['input_mask']
  segment_ids = inputs['segment_ids']

  labels = inputs['labels']
  additional_labels = {}
  for additional_label in _IDENTITY_LABELS:
    if additional_label in inputs:
      additional_labels[additional_label] = inputs[additional_label]

  return [input_ids, input_mask, segment_ids], labels, additional_labels


def main(argv):
  del argv  # unused arg
  tf.io.gfile.makedirs(FLAGS.output_dir)
  logging.info('Saving checkpoints at %s', FLAGS.output_dir)
  tf.random.set_seed(FLAGS.seed)

  if FLAGS.use_gpu:
    logging.info('Use GPU')
    strategy = tf.distribute.MirroredStrategy()
  else:
    logging.info('Use TPU at %s',
                 FLAGS.tpu if FLAGS.tpu is not None else 'local')
    resolver = tf.distribute.cluster_resolver.TPUClusterResolver(tpu=FLAGS.tpu)
    tf.config.experimental_connect_to_cluster(resolver)
    tf.tpu.experimental.initialize_tpu_system(resolver)
    strategy = tf.distribute.experimental.TPUStrategy(resolver)

  batch_size = FLAGS.per_core_batch_size * FLAGS.num_cores
  test_batch_size = batch_size
  data_buffer_size = batch_size * 10

  train_dataset_builder = ds.WikipediaToxicityDataset(
      batch_size=FLAGS.per_core_batch_size,
      eval_batch_size=FLAGS.per_core_batch_size,
      data_dir=FLAGS.in_dataset_dir,
      shuffle_buffer_size=data_buffer_size)
  ind_dataset_builder = ds.WikipediaToxicityDataset(
      batch_size=FLAGS.per_core_batch_size,
      eval_batch_size=FLAGS.per_core_batch_size,
      data_dir=FLAGS.in_dataset_dir,
      shuffle_buffer_size=data_buffer_size)
  ood_dataset_builder = ds.CivilCommentsDataset(
      batch_size=FLAGS.per_core_batch_size,
      eval_batch_size=FLAGS.per_core_batch_size,
      data_dir=FLAGS.ood_dataset_dir,
      shuffle_buffer_size=data_buffer_size)
  ood_identity_dataset_builder = ds.CivilCommentsIdentitiesDataset(
      batch_size=FLAGS.per_core_batch_size,
      eval_batch_size=FLAGS.per_core_batch_size,
      data_dir=FLAGS.identity_dataset_dir,
      shuffle_buffer_size=data_buffer_size)

  dataset_builders = {
      'ind': ind_dataset_builder,
      'ood': ood_dataset_builder,
      'ood_identity': ood_identity_dataset_builder,
  }

  train_dataset = train_dataset_builder.build(split=base.Split.TRAIN)

  ds_info = train_dataset_builder.info
  num_classes = ds_info['num_classes']  # Positive and negative classes.

  steps_per_epoch = ds_info['num_train_examples'] // batch_size

  test_datasets = {}
  steps_per_eval = {}
  for dataset_name, dataset_builder in dataset_builders.items():
    test_datasets[dataset_name] = dataset_builder.build(split=base.Split.TEST)
    steps_per_eval[dataset_name] = (
        dataset_builder.info['num_test_examples'] // test_batch_size)

  if FLAGS.use_bfloat16:
    policy = tf.keras.mixed_precision.experimental.Policy('mixed_bfloat16')
    tf.keras.mixed_precision.experimental.set_policy(policy)

  summary_writer = tf.summary.create_file_writer(
      os.path.join(FLAGS.output_dir, 'summaries'))

  with strategy.scope():
    logging.info('Building BERT %s model', FLAGS.bert_model_type)
    logging.info('use_gp_layer=%s', FLAGS.use_gp_layer)
    logging.info('use_spec_norm_att=%s', FLAGS.use_spec_norm_att)
    logging.info('use_spec_norm_ffn=%s', FLAGS.use_spec_norm_ffn)
    logging.info('use_layer_norm_att=%s', FLAGS.use_layer_norm_att)
    logging.info('use_layer_norm_ffn=%s', FLAGS.use_layer_norm_ffn)

    bert_config_dir, bert_ckpt_dir = resolve_bert_ckpt_and_config_dir(
        FLAGS.bert_dir, FLAGS.bert_config_dir, FLAGS.bert_ckpt_dir)
    bert_config = bert_utils.create_config(bert_config_dir)

    gp_layer_kwargs = dict(
        num_inducing=FLAGS.gp_hidden_dim,
        gp_kernel_scale=FLAGS.gp_scale,
        gp_output_bias=FLAGS.gp_bias,
        normalize_input=FLAGS.gp_input_normalization,
        gp_cov_momentum=FLAGS.gp_cov_discount_factor,
        gp_cov_ridge_penalty=FLAGS.gp_cov_ridge_penalty)
    spec_norm_kwargs = dict(
        iteration=FLAGS.spec_norm_iteration,
        norm_multiplier=FLAGS.spec_norm_bound)

    model, bert_encoder = ub.models.SngpBertBuilder(
        num_classes=num_classes,
        bert_config=bert_config,
        gp_layer_kwargs=gp_layer_kwargs,
        spec_norm_kwargs=spec_norm_kwargs,
        use_gp_layer=FLAGS.use_gp_layer,
        use_spec_norm_att=FLAGS.use_spec_norm_att,
        use_spec_norm_ffn=FLAGS.use_spec_norm_ffn,
        use_layer_norm_att=FLAGS.use_layer_norm_att,
        use_layer_norm_ffn=FLAGS.use_layer_norm_ffn,
        use_spec_norm_plr=FLAGS.use_spec_norm_plr)
    optimizer = bert_utils.create_optimizer(
        FLAGS.base_learning_rate,
        steps_per_epoch=steps_per_epoch,
        epochs=FLAGS.train_epochs,
        warmup_proportion=FLAGS.warmup_proportion)

    logging.info('Model input shape: %s', model.input_shape)
    logging.info('Model output shape: %s', model.output_shape)
    logging.info('Model number of weights: %s', model.count_params())

    metrics = {
        'train/negative_log_likelihood': tf.keras.metrics.Mean(),
        'train/accuracy': tf.keras.metrics.Accuracy(),
        'train/loss': tf.keras.metrics.Mean(),
        'train/ece': um.ExpectedCalibrationError(num_bins=FLAGS.num_bins),
    }

    checkpoint = tf.train.Checkpoint(model=model, optimizer=optimizer)
    if FLAGS.prediction_mode:
      latest_checkpoint = tf.train.latest_checkpoint(FLAGS.eval_checkpoint_dir)
    else:
      latest_checkpoint = tf.train.latest_checkpoint(FLAGS.output_dir)
    initial_epoch = 0
    if latest_checkpoint:
      # checkpoint.restore must be within a strategy.scope() so that optimizer
      # slot variables are mirrored.
      checkpoint.restore(latest_checkpoint)
      logging.info('Loaded checkpoint %s', latest_checkpoint)
      initial_epoch = optimizer.iterations.numpy() // steps_per_epoch
    else:
      # load BERT from initial checkpoint
      bert_encoder, _, _ = bert_utils.load_bert_weight_from_ckpt(
          bert_model=bert_encoder,
          bert_ckpt_dir=bert_ckpt_dir,
          repl_patterns=ub.models.bert_sngp.CHECKPOINT_REPL_PATTERNS)
      logging.info('Loaded BERT checkpoint %s', bert_ckpt_dir)

    # Finally, define test metrics outside the accelerator scope for CPU eval.
    metrics.update({
        'test/nll': tf.keras.metrics.Mean(),
        'test/auroc': tf.keras.metrics.AUC(curve='ROC'),
        'test/aupr': tf.keras.metrics.AUC(curve='PR'),
        'test/brier': tf.keras.metrics.MeanSquaredError(),
        'test/ece': um.ExpectedCalibrationError(num_bins=FLAGS.num_bins),
        'test/eval_time': tf.keras.metrics.Mean(),
        'test/stddev': tf.keras.metrics.Mean(),
        'test/acc': tf.keras.metrics.Accuracy(),
    })
    for fraction in FLAGS.fractions:
      metrics.update({
          'test_collab_acc/collab_acc_{}'.format(fraction):
              um.OracleCollaborativeAccuracy(
                  fraction=float(fraction), num_bins=FLAGS.num_bins)
      })
    for dataset_name, test_dataset in test_datasets.items():
      if dataset_name != 'ind':
        metrics.update({
            'test/nll_{}'.format(dataset_name):
                tf.keras.metrics.Mean(),
            'test/auroc_{}'.format(dataset_name):
                tf.keras.metrics.AUC(curve='ROC'),
            'test/aupr_{}'.format(dataset_name):
                tf.keras.metrics.AUC(curve='PR'),
            'test/brier_{}'.format(dataset_name):
                tf.keras.metrics.MeanSquaredError(),
            'test/ece_{}'.format(dataset_name):
                um.ExpectedCalibrationError(num_bins=FLAGS.num_bins),
            'test/eval_time_{}'.format(dataset_name):
                tf.keras.metrics.Mean(),
            'test/stddev_{}'.format(dataset_name):
                tf.keras.metrics.Mean(),
            'test/acc_{}'.format(dataset_name):
                tf.keras.metrics.Accuracy()
        })
        for fraction in FLAGS.fractions:
          metrics.update({
              'test_collab_acc/collab_acc_{}_{}'.format(fraction, dataset_name):
                  um.OracleCollaborativeAccuracy(
                      fraction=float(fraction), num_bins=FLAGS.num_bins)
          })

  @tf.function
  def train_step(iterator):
    """Training StepFn."""

    def step_fn(inputs):
      """Per-Replica StepFn."""
      features, labels, _ = create_feature_and_label(inputs)

      with tf.GradientTape() as tape:
        logits = model(features, training=True)

        if isinstance(logits, tuple):
          # If model returns a tuple of (logits, covmat), extract logits
          logits, _ = logits
        if FLAGS.use_bfloat16:
          logits = tf.cast(logits, tf.float32)

        loss_logits = tf.squeeze(logits, axis=1)
        if FLAGS.loss_type == 'cross_entropy':
          logging.info('Using cross entropy loss')
          negative_log_likelihood = tf.nn.sigmoid_cross_entropy_with_logits(
              labels, loss_logits)
        elif FLAGS.loss_type == 'mse':
          logging.info('Using mean squared error loss')
          loss_probs = tf.nn.sigmoid(loss_logits)
          negative_log_likelihood = tf.keras.losses.mean_squared_error(
              labels, loss_probs)
        elif FLAGS.loss_type == 'mae':
          logging.info('Using mean absolute error loss')
          loss_probs = tf.nn.sigmoid(loss_logits)
          negative_log_likelihood = tf.keras.losses.mean_absolute_error(
              labels, loss_probs)

        negative_log_likelihood = tf.reduce_mean(negative_log_likelihood)

        l2_loss = sum(model.losses)
        loss = negative_log_likelihood + l2_loss
        # Scale the loss given the TPUStrategy will reduce sum all gradients.
        scaled_loss = loss / strategy.num_replicas_in_sync

      grads = tape.gradient(scaled_loss, model.trainable_variables)
      optimizer.apply_gradients(zip(grads, model.trainable_variables))

      probs = tf.nn.sigmoid(logits)
      # Cast labels to discrete for ECE computation.
      ece_labels = tf.cast(labels > FLAGS.ece_label_threshold, tf.float32)
      ece_probs = tf.concat([1. - probs, probs], axis=1)
      pred_labels = tf.math.argmax(ece_probs, axis=-1)

      metrics['train/accuracy'].update_state(labels, pred_labels)
      metrics['train/ece'].update_state(ece_labels, ece_probs)
      metrics['train/loss'].update_state(loss)
      metrics['train/negative_log_likelihood'].update_state(
          negative_log_likelihood)

    strategy.run(step_fn, args=(next(iterator),))

  @tf.function
  def test_step(iterator, dataset_name):
    """Evaluation StepFn."""

    def step_fn(inputs):
      """Per-Replica StepFn."""
      features, labels, _ = create_feature_and_label(inputs)

      # Compute ensemble prediction over Monte Carlo forward-pass samples.
      logits_list = []
      stddev_list = []
      for _ in range(FLAGS.num_mc_samples):
        logits = model(features, training=False)

        if isinstance(logits, tuple):
          # If model returns a tuple of (logits, covmat), extract both.
          logits, covmat = logits
        else:
          covmat = tf.eye(test_batch_size)

        if FLAGS.use_bfloat16:
          logits = tf.cast(logits, tf.float32)
          covmat = tf.cast(covmat, tf.float32)

        logits = ed.layers.utils.mean_field_logits(
            logits, covmat, mean_field_factor=FLAGS.gp_mean_field_factor)
        stddev = tf.sqrt(tf.linalg.diag_part(covmat))

        logits_list.append(logits)
        stddev_list.append(stddev)

      # Logits dimension is (num_samples, batch_size, num_classes).
      logits_list = tf.stack(logits_list, axis=0)
      stddev_list = tf.stack(stddev_list, axis=0)

      stddev = tf.reduce_mean(stddev_list, axis=0)
      probs_list = tf.nn.sigmoid(logits_list)
      probs = tf.reduce_mean(probs_list, axis=0)
      # Cast labels to discrete for ECE computation.
      ece_labels = tf.cast(labels > FLAGS.ece_label_threshold, tf.float32)
      ece_probs = tf.concat([1. - probs, probs], axis=1)
      pred_labels = tf.math.argmax(ece_probs, axis=-1)
      auc_probs = tf.squeeze(probs, axis=1)

      ce = tf.nn.sigmoid_cross_entropy_with_logits(
          labels=tf.broadcast_to(
              labels, [FLAGS.num_mc_samples, labels.shape[0]]),
          logits=tf.squeeze(logits_list, axis=-1)
      )
      negative_log_likelihood = -tf.reduce_logsumexp(
          -ce, axis=0) + tf.math.log(float(FLAGS.num_mc_samples))
      negative_log_likelihood = tf.reduce_mean(negative_log_likelihood)

      if dataset_name == 'ind':
        metrics['test/nll'].update_state(
            negative_log_likelihood)
        metrics['test/auroc'].update_state(labels, auc_probs)
        metrics['test/aupr'].update_state(labels, auc_probs)
        metrics['test/brier'].update_state(labels, auc_probs)
        metrics['test/ece'].update_state(ece_labels, ece_probs)
        metrics['test/stddev'].update_state(stddev)
        metrics['test/acc'].update_state(ece_labels, pred_labels)
        for fraction in FLAGS.fractions:
          metrics['test_collab_acc/collab_acc_{}'.format(
              fraction)].update_state(ece_labels, ece_probs)
      else:
        metrics['test/nll_{}'.format(dataset_name)].update_state(
            negative_log_likelihood)
        metrics['test/auroc_{}'.format(dataset_name)].update_state(
            labels, auc_probs)
        metrics['test/aupr_{}'.format(dataset_name)].update_state(
            labels, auc_probs)
        metrics['test/brier_{}'.format(dataset_name)].update_state(
            labels, auc_probs)
        metrics['test/ece_{}'.format(dataset_name)].update_state(
            ece_labels, ece_probs)
        metrics['test/stddev_{}'.format(dataset_name)].update_state(stddev)
        metrics['test/acc_{}'.format(dataset_name)].update_state(
            ece_labels, pred_labels
        )
        for fraction in FLAGS.fractions:
          metrics['test_collab_acc/collab_acc_{}_{}'.format(
              fraction, dataset_name)].update_state(ece_labels, ece_probs)

    strategy.run(step_fn, args=(next(iterator),))

  @tf.function
  def final_eval_step(iterator):
    """Final Evaluation StepFn to save prediction to directory."""

    def step_fn(inputs):
      bert_features, labels, additional_labels = create_feature_and_label(
          inputs)
      logits = model(bert_features, training=False)
      if isinstance(logits, tuple):
        # If model returns a tuple of (logits, covmat), extract both.
        logits, covmat = logits
      else:
        covmat = tf.eye(test_batch_size)

      if FLAGS.use_bfloat16:
        logits = tf.cast(logits, tf.float32)
        covmat = tf.cast(covmat, tf.float32)

      logits = ed.layers.utils.mean_field_logits(
          logits, covmat, mean_field_factor=FLAGS.gp_mean_field_factor)
      features = inputs['input_ids']
      return features, logits, labels, additional_labels

    (per_replica_texts, per_replica_logits, per_replica_labels,
     per_replica_additional_labels) = (
         strategy.run(step_fn, args=(next(iterator),)))

    if strategy.num_replicas_in_sync > 1:
      texts_list = tf.concat(per_replica_texts.values, axis=0)
      logits_list = tf.concat(per_replica_logits.values, axis=0)
      labels_list = tf.concat(per_replica_labels.values, axis=0)
      additional_labels_dict = {}
      for additional_label in _IDENTITY_LABELS:
        if additional_label in per_replica_additional_labels:
          additional_labels_dict[additional_label] = tf.concat(
              per_replica_additional_labels[additional_label], axis=0)
    else:
      texts_list = per_replica_texts
      logits_list = per_replica_logits
      labels_list = per_replica_labels
      additional_labels_dict = {}
      for additional_label in _IDENTITY_LABELS:
        if additional_label in per_replica_additional_labels:
          additional_labels_dict[
              additional_label] = per_replica_additional_labels[
                  additional_label]

    return texts_list, logits_list, labels_list, additional_labels_dict

  if FLAGS.prediction_mode:
    # Prediction and exit.
    for dataset_name, test_dataset in test_datasets.items():
      test_iterator = iter(test_dataset)  # pytype: disable=wrong-arg-types
      message = 'Final eval on dataset {}'.format(dataset_name)
      logging.info(message)

      texts_all = []
      logits_all = []
      labels_all = []
      additional_labels_all_dict = {}
      if 'identity' in dataset_name:
        for identity_label_name in _IDENTITY_LABELS:
          additional_labels_all_dict[identity_label_name] = []

      for step in range(steps_per_eval[dataset_name]):
        if step % 20 == 0:
          message = 'Starting to run eval step {}/{} of dataset: {}'.format(
              step, steps_per_eval[dataset_name], dataset_name)
          logging.info(message)

        try:
          (text_step, logits_step, labels_step,
           additional_labels_dict_step) = final_eval_step(test_iterator)
        except tf.errors.OutOfRangeError:
          continue

        texts_all.append(text_step)
        logits_all.append(logits_step)
        labels_all.append(labels_step)
        if 'identity' in dataset_name:
          for identity_label_name in _IDENTITY_LABELS:
            additional_labels_all_dict[identity_label_name].append(
                additional_labels_dict_step[identity_label_name])

      texts_all = tf.concat(texts_all, axis=0)
      logits_all = tf.concat(logits_all, axis=0)
      labels_all = tf.concat(labels_all, axis=0)
      additional_labels_all = []
      if additional_labels_all_dict:
        for identity_label_name in _IDENTITY_LABELS:
          additional_labels_all.append(
              tf.concat(
                  additional_labels_all_dict[identity_label_name], axis=0))
      additional_labels_all = tf.convert_to_tensor(additional_labels_all)

      save_prediction(
          texts_all.numpy(),
          path=os.path.join(FLAGS.output_dir, 'texts_{}'.format(dataset_name)))
      save_prediction(
          labels_all.numpy(),
          path=os.path.join(FLAGS.output_dir, 'labels_{}'.format(dataset_name)))
      save_prediction(
          logits_all.numpy(),
          path=os.path.join(FLAGS.output_dir, 'logits_{}'.format(dataset_name)))
      if 'identity' in dataset_name:
        save_prediction(
            additional_labels_all.numpy(),
            path=os.path.join(FLAGS.output_dir,
                              'additional_labels_{}'.format(dataset_name)))
      logging.info('Done with testing on %s', dataset_name)

  else:
    train_iterator = iter(train_dataset)
    start_time = time.time()
    for epoch in range(initial_epoch, FLAGS.train_epochs):
      logging.info('Starting to run epoch: %s', epoch)
      for step in range(steps_per_epoch):
        train_step(train_iterator)

        current_step = epoch * steps_per_epoch + (step + 1)
        max_steps = steps_per_epoch * FLAGS.train_epochs
        time_elapsed = time.time() - start_time
        steps_per_sec = float(current_step) / time_elapsed
        eta_seconds = (max_steps - current_step) / steps_per_sec
        message = ('{:.1%} completion: epoch {:d}/{:d}. {:.1f} steps/s. '
                   'ETA: {:.0f} min. Time elapsed: {:.0f} min'.format(
                       current_step / max_steps, epoch + 1, FLAGS.train_epochs,
                       steps_per_sec, eta_seconds / 60, time_elapsed / 60))
        if step % 20 == 0:
          logging.info(message)

      if epoch % FLAGS.evaluation_interval == 0:
        for dataset_name, test_dataset in test_datasets.items():
          test_iterator = iter(test_dataset)
          logging.info('Testing on dataset %s', dataset_name)
          for step in range(steps_per_eval[dataset_name]):
            if step % 20 == 0:
              logging.info('Starting to run eval step %s of epoch: %s', step,
                           epoch)
            test_step(test_iterator, dataset_name)
          logging.info('Done with testing on %s', dataset_name)

        logging.info('Train Loss: %.4f, ECE: %.2f, Accuracy: %.2f',
                     metrics['train/loss'].result(),
                     metrics['train/ece'].result(),
                     metrics['train/accuracy'].result())

        total_results = {
            name: metric.result() for name, metric in metrics.items()
        }
        with summary_writer.as_default():
          for name, result in total_results.items():
            tf.summary.scalar(name, result, step=epoch + 1)

      for metric in metrics.values():
        metric.reset_states()

      if (FLAGS.checkpoint_interval > 0 and
          (epoch + 1) % FLAGS.checkpoint_interval == 0):
        checkpoint_name = checkpoint.save(
            os.path.join(FLAGS.output_dir, 'checkpoint'))
        logging.info('Saved checkpoint to %s', checkpoint_name)

    # Save model in SavedModel format on exit.
    final_save_name = os.path.join(FLAGS.output_dir, 'model')
    model.save(final_save_name)
    logging.info('Saved model to %s', final_save_name)


if __name__ == '__main__':
  app.run(main)
