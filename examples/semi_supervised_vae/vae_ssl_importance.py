#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import print_function
from __future__ import division
import os
import time

import tensorflow as tf
from six.moves import range
import numpy as np
import zhusuan as zs

from examples import conf
from examples.utils import dataset


@zs.meta_bayesian_net(scope="gen", reuse_variables=True)
def build_gen(n, n_x, n_y, n_z, n_particles):
    bn = zs.BayesianNet()
    z_mean = tf.zeros([n, n_z])
    z = bn.normal("z", z_mean, std=1., n_samples=n_particles, group_ndims=1)
    h_from_z = tf.layers.dense(z, 500)
    y_logits = tf.zeros([n, n_y])
    y = bn.onehot_categorical("y", y_logits, n_samples=n_particles)
    h_from_y = tf.layers.dense(tf.to_float(y), 500)
    h = tf.nn.relu(h_from_z + h_from_y)
    h = tf.layers.dense(h, 500, activation=tf.nn.relu)
    x_logits = tf.layers.dense(h, n_x)
    bn.bernoulli('x', x_logits, group_ndims=1)
    return bn


@zs.reuse_variables(scope="qz_xy")
def qz_xy(x, y, n_z):
    lz_xy = tf.layers.dense(tf.to_float(tf.concat([x, y], -1)), 500,
                            activation=tf.nn.relu)
    lz_xy = tf.layers.dense(lz_xy, 500, activation=tf.nn.relu)
    z_mean = tf.layers.dense(lz_xy, n_z)
    z_logstd = tf.layers.dense(lz_xy, n_z)
    return z_mean, z_logstd


@zs.reuse_variables(scope="qy_x")
def qy_x(x, n_y):
    ly_x = tf.layers.dense(tf.to_float(x), 500, activation=tf.nn.relu)
    ly_x = tf.layers.dense(ly_x, 500, activation=tf.nn.relu)
    y_logits = tf.layers.dense(ly_x, n_y)
    return y_logits


@zs.meta_bayesian_net()
def labeled_proposal(x, y, n_z, n_particles):
    bn = zs.BayesianNet()
    z_mean, z_logstd = qz_xy(x, y, n_z)
    bn.normal("z", z_mean, logstd=z_logstd, n_samples=n_particles,
              group_ndims=1, is_reparameterized=False)
    return bn


@zs.meta_bayesian_net()
def unlabeled_proposal(x, n_y, n_z, n_particles):
    bn = zs.BayesianNet()
    y_logits = qy_x(x, n_y)
    y = bn.onehot_categorical("y", y_logits, n_samples=n_particles)
    x_tiled = tf.tile(x[None, ...], [n_particles, 1, 1])
    z_mean, z_logstd = qz_xy(x_tiled, y, n_z)
    bn.normal("z", z_mean, logstd=z_logstd, group_ndims=1,
              is_reparameterized=False)
    return bn


def main():
    tf.set_random_seed(1234)
    np.random.seed(1234)

    # Load MNIST
    data_path = os.path.join(conf.data_dir, 'mnist.pkl.gz')
    x_labeled, t_labeled, x_unlabeled, x_test, t_test = \
        dataset.load_mnist_semi_supervised(data_path, one_hot=True)
    x_test = np.random.binomial(1, x_test, size=x_test.shape).astype('float32')
    n_labeled, n_x = x_labeled.shape
    n_y = 10

    # Define model parameters
    n_z = 100

    # Define training/evaluation parameters
    ll_samples = 10
    beta = 1200.
    epochs = 3000
    batch_size = 100
    test_batch_size = 100
    iters = x_unlabeled.shape[0] // batch_size
    test_iters = x_test.shape[0] // test_batch_size
    test_freq = 10
    learning_rate = 0.0003
    anneal_lr_freq = 200
    anneal_lr_rate = 0.75

    # Build the computation graph
    n = tf.placeholder(tf.int32, shape=[], name="n")
    n_particles = tf.placeholder(tf.int32, shape=[], name="n_particles")
    x_orig = tf.placeholder(tf.float32, shape=[None, n_x], name="x")
    x_bin = tf.cast(tf.less(tf.random_uniform(tf.shape(x_orig), 0, 1), x_orig),
                    tf.int32)

    gen = build_gen(n, n_x, n_y, n_z, n_particles)

    # Labeled
    x_labeled_ph = tf.placeholder(tf.int32, shape=(None, n_x), name="x_l")
    y_labeled_ph = tf.placeholder(tf.int32, shape=(None, n_y), name="y_l")
    proposal = labeled_proposal(x_labeled_ph, y_labeled_ph, n_z, n_particles)

    # adapting the proposal
    labeled_klpq_obj = zs.variational.klpq(
        gen,
        observed={"x": x_labeled_ph, "y": y_labeled_ph},
        variational=proposal,
        axis=0)
    labeled_q_cost = tf.reduce_mean(labeled_klpq_obj.importance())

    # learning model parameters
    labeled_lower_bound = tf.reduce_mean(
        zs.variational.importance_weighted_objective(
            gen, observed={'x': x_labeled_ph, 'y': y_labeled_ph},
            variational=proposal, axis=0))

    # Unlabeled
    x_unlabeled_ph = tf.placeholder(tf.int32, shape=(None, n_x), name="x_u")
    proposal = unlabeled_proposal(x_unlabeled_ph, n_y, n_z, n_particles)

    # adapting the proposal
    unlabeled_klpq_obj = zs.variational.klpq(
        gen,
        observed={'x': x_unlabeled_ph},
        variational=proposal,
        axis=0)
    unlabeled_q_cost = tf.reduce_mean(unlabeled_klpq_obj.importance())

    # learning model parameters
    unlabeled_lower_bound = tf.reduce_mean(
        zs.variational.importance_weighted_objective(
            gen, observed={'x': x_unlabeled_ph}, variational=proposal, axis=0))

    # Build classifier
    qy_logits_l = qy_x(x_labeled_ph, n_y)
    qy_l = tf.nn.softmax(qy_logits_l)
    pred_y = tf.argmax(qy_l, 1)
    acc = tf.reduce_sum(
        tf.cast(tf.equal(pred_y, tf.argmax(y_labeled_ph, 1)), tf.float32) /
        tf.cast(tf.shape(x_labeled_ph)[0], tf.float32))
    onehot_cat = zs.distributions.OnehotCategorical(qy_logits_l)
    log_qy_x = onehot_cat.log_prob(y_labeled_ph)
    classifier_cost = -beta * tf.reduce_mean(log_qy_x)

    # Gather gradients
    proposal_cost = labeled_q_cost + unlabeled_q_cost + classifier_cost
    model_cost = -labeled_lower_bound - unlabeled_lower_bound

    learning_rate_ph = tf.placeholder(tf.float32, shape=[], name="lr")
    optimizer = tf.train.AdamOptimizer(learning_rate_ph)
    model_params = tf.trainable_variables(scope="gen")
    model_grads = optimizer.compute_gradients(model_cost, var_list=model_params)
    proposal_params = (tf.trainable_variables(scope="qy_x") +
                       tf.trainable_variables(scope="qz_xy"))
    proposal_grads = optimizer.compute_gradients(proposal_cost,
                                                 var_list=proposal_params)
    infer_op = optimizer.apply_gradients(model_grads + proposal_grads)

    params = tf.trainable_variables()
    for i in params:
        print(i.name, i.get_shape())

    # Run the inference
    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        for epoch in range(1, epochs + 1):
            time_epoch = -time.time()
            if epoch % anneal_lr_freq == 0:
                learning_rate *= anneal_lr_rate
            np.random.shuffle(x_unlabeled)
            lbs_labeled, lbs_unlabeled, train_accs = [], [], []

            for t in range(iters):
                labeled_indices = np.random.randint(0, n_labeled,
                                                    size=batch_size)
                x_labeled_batch = x_labeled[labeled_indices]
                y_labeled_batch = t_labeled[labeled_indices]
                x_unlabeled_batch = x_unlabeled[t * batch_size:
                                                (t + 1) * batch_size]
                x_labeled_batch_bin = sess.run(
                    x_bin, feed_dict={x_orig: x_labeled_batch})
                x_unlabeled_batch_bin = sess.run(
                    x_bin, feed_dict={x_orig: x_unlabeled_batch})
                _, lb_labeled, lb_unlabeled, train_acc = sess.run(
                    [infer_op, labeled_lower_bound, unlabeled_lower_bound,
                     acc],
                    feed_dict={x_labeled_ph: x_labeled_batch_bin,
                               y_labeled_ph: y_labeled_batch,
                               x_unlabeled_ph: x_unlabeled_batch_bin,
                               learning_rate_ph: learning_rate,
                               n_particles: ll_samples,
                               n: batch_size})
                lbs_labeled.append(lb_labeled)
                lbs_unlabeled.append(lb_unlabeled)
                train_accs.append(train_acc)

            time_epoch += time.time()
            print('Epoch {} ({:.1f}s), Lower bound: labeled = {}, '
                  'unlabeled = {} Accuracy: {:.2f}%'.
                  format(epoch, time_epoch, np.mean(lbs_labeled),
                         np.mean(lbs_unlabeled), np.mean(train_accs) * 100.))

            if epoch % test_freq == 0:
                time_test = -time.time()
                test_lls_labeled, test_lls_unlabeled, test_accs = [], [], []

                for t in range(test_iters):
                    test_x_batch = x_test[
                        t * test_batch_size: (t + 1) * test_batch_size]
                    test_y_batch = t_test[
                        t * test_batch_size: (t + 1) * test_batch_size]
                    test_ll_labeled, test_ll_unlabeled, test_acc = sess.run(
                        [labeled_lower_bound, unlabeled_lower_bound,
                         acc],
                        feed_dict={x_labeled_ph: test_x_batch,
                                   y_labeled_ph: test_y_batch,
                                   x_unlabeled_ph: test_x_batch,
                                   n_particles: ll_samples,
                                   n: test_batch_size})
                    test_lls_labeled.append(test_ll_labeled)
                    test_lls_unlabeled.append(test_ll_unlabeled)
                    test_accs.append(test_acc)

                time_test += time.time()
                print('>>> TEST ({:.1f}s)'.format(time_test))
                print('>> Test lower bound: labeled = {}, unlabeled = {}'.
                      format(np.mean(test_lls_labeled),
                             np.mean(test_lls_unlabeled)))
                print('>> Test accuracy: {:.2f}%'.format(
                    100. * np.mean(test_accs)))


if __name__ == "__main__":
    main()