#!/usr/bin/env python3
import gym
import ptan
import numpy as np
import argparse
from tensorboardX import SummaryWriter

import torch
import torch.nn.utils as nn_utils
import torch.nn.functional as F
import torch.optim as optim
import torch.multiprocessing as mp
from torch.autograd import Variable

from lib import common

GAMMA = 0.99
LEARNING_RATE = 0.001
ENTROPY_BETA = 0.01

REWARD_STEPS = 4
CLIP_GRAD = 0.1

PROCESSES_COUNT = 3
NUM_ENVS = 15

GRAD_BATCH = 4
TRAIN_BATCH = 16


if True:
    ENV_NAME = "PongNoFrameskip-v4"
    NAME = 'pong'
    REWARD_BOUND = 18
else:
    ENV_NAME = "BreakoutNoFrameskip-v4"
    NAME = "breakout"
    REWARD_BOUND = 400


def make_env():
    return ptan.common.wrappers.wrap_dqn(gym.make(ENV_NAME))


class CachingA2CAgent(ptan.agent.BaseAgent):
    def __init__(self, model, cuda=False, preprocessor=ptan.agent.default_states_preprocessor):
        self.model = model
        self.cuda = cuda
        self.values_cache = {}
        self.cache = {}
        self.action_selector = ptan.actions.ProbabilityActionSelector()
        self.preprocessor = preprocessor

    def __call__(self, states, agent_states=None):
        if agent_states is None:
            agent_states = [None] * states.shape[0]
        if self.preprocessor is not None:
            prep_states = self.preprocessor(states)
        v = Variable(torch.from_numpy(prep_states))
        if self.cuda:
            v = v.cuda()
        logits_v, values_v = self.model(v)
        probs_v = F.softmax(logits_v)
        probs = probs_v.data.cpu().numpy()
        actions = self.action_selector(probs)

        values = values_v.data.cpu().numpy().squeeze()
        for ofs, (state, value) in enumerate(zip(states, values)):
            self.values_cache[id(state)] = value
            self.cache[id(state)] = logits_v[ofs], values_v[ofs]

        return np.array(actions), agent_states


def data_func(proc_name, net, cuda, train_queue, batch_size=GRAD_BATCH):
    envs = [make_env() for _ in range(NUM_ENVS)]

#    agent = ptan.agent.PolicyAgent(lambda x: net(x)[0], cuda=cuda, apply_softmax=True)
    agent = CachingA2CAgent(net, cuda)
    exp_source = ptan.experience.ExperienceSourceFirstLast(envs, agent, gamma=GAMMA, steps_count=REWARD_STEPS)

    batch = []
    frame_idx = 0
    writer = SummaryWriter(comment=proc_name)

    with common.RewardTracker(writer, stop_reward=REWARD_BOUND) as tracker:
        with ptan.common.utils.TBMeanTracker(writer, batch_size=100) as tb_tracker:
            for exp in exp_source:
                frame_idx += 1
                new_rewards = exp_source.pop_total_rewards()
                if new_rewards and tracker.reward(new_rewards[0], frame_idx):
                    break

                if exp.last_state is not None:
                    reward = exp.reward + (GAMMA ** REWARD_STEPS) * agent.values_cache[id(exp.last_state)]
                    exp = ptan.experience.ExperienceFirstLast(state=exp.state, action=exp.action, reward=reward, last_state=None)

                batch.append(exp)
                if len(batch) < batch_size:
                    continue

                states_v, actions_t, vals_ref_v = unpack_batch(batch, net, cuda=cuda)
                batch.clear()

                net.zero_grad()
                logits_v, value_v = net(states_v)

                loss_value_v = F.mse_loss(value_v, vals_ref_v)

                log_prob_v = F.log_softmax(logits_v)
                adv_v = vals_ref_v - value_v.detach()
                log_prob_actions_v = adv_v * log_prob_v[range(batch_size), actions_t]
                loss_policy_v = -log_prob_actions_v.mean()

                prob_v = F.softmax(logits_v)
                entropy_loss_v = ENTROPY_BETA * (prob_v * log_prob_v).sum(dim=1).mean()

                # apply entropy and value gradients
                loss_v = entropy_loss_v + loss_value_v + loss_policy_v
                loss_v.backward()

                tb_tracker.track("advantage", adv_v, frame_idx)
                tb_tracker.track("values", value_v, frame_idx)
                tb_tracker.track("batch_rewards", vals_ref_v, frame_idx)
                tb_tracker.track("loss_entropy", entropy_loss_v, frame_idx)
                tb_tracker.track("loss_policy", loss_policy_v, frame_idx)
                tb_tracker.track("loss_value", loss_value_v, frame_idx)
                tb_tracker.track("loss_total", loss_v, frame_idx)

                # gather gradients
                grads = [param.grad.data.cpu().numpy() if param.grad is not None else None
                         for param in net.parameters()]
                train_queue.put(grads)

    train_queue.put(None)


def unpack_batch(batch, net, cuda=False):
    """
    Convert batch into training tensors
    :param batch:
    :param net:
    :return: states variable, actions tensor, reference values variable
    """
    states = []
    actions = []
    rewards = []
    not_done_idx = []
    last_states = []
    for idx, exp in enumerate(batch):
        states.append(np.array(exp.state, copy=False))
        actions.append(int(exp.action))
        rewards.append(exp.reward)
        if exp.last_state is not None:
            not_done_idx.append(idx)
            last_states.append(np.array(exp.last_state, copy=False))
    states_v = Variable(torch.from_numpy(np.array(states, copy=False)))
    actions_t = torch.LongTensor(actions)
    if cuda:
        states_v = states_v.cuda()
        actions_t = actions_t.cuda()

    # handle rewards
    rewards_np = np.array(rewards, dtype=np.float32)
    # if not_done_idx:
    #     last_states_v = Variable(torch.from_numpy(np.array(last_states, copy=False)), volatile=True)
    #     if cuda:
    #         last_states_v = last_states_v.cuda()
    #     last_vals_v = net(last_states_v)[1]
    #     last_vals_np = last_vals_v.data.cpu().numpy()[:, 0]
    #     rewards_np[not_done_idx] += GAMMA ** REWARD_STEPS * last_vals_np

    ref_vals_v = Variable(torch.from_numpy(rewards_np))
    if cuda:
        ref_vals_v = ref_vals_v.cuda()

    return states_v, actions_t, ref_vals_v


if __name__ == "__main__":
    mp.set_start_method('spawn')
    parser = argparse.ArgumentParser()
    parser.add_argument("--cuda", default=False, action="store_true", help="Enable cuda")
    parser.add_argument("-n", "--name", required=True, help="Name of the run")
    args = parser.parse_args()

    env = make_env()
    net = common.AtariA2C(env.observation_space.shape, env.action_space.n)
    if args.cuda:
        net.cuda()

    optimizer = optim.Adam(net.parameters(), lr=LEARNING_RATE, eps=1e-3)

    train_queue = mp.Queue(maxsize=PROCESSES_COUNT*10)
    data_proc_list = []
    tgt_nets = []
    for proc_idx in range(PROCESSES_COUNT):
        tgt_net = ptan.agent.TargetNet(net)
        tgt_nets.append(tgt_net)
        proc_name = "-a3c-grad_" + NAME + "_" + args.name + "#%d" % proc_idx
        data_proc = mp.Process(target=data_func, args=(proc_name, tgt_net.target_model, args.cuda, train_queue))
        data_proc.start()
        data_proc_list.append(data_proc)

    batch = []
    step_idx = 0
    grad_buffer = None

    while True:
        train_entry = train_queue.get()
        if train_entry is None:
            break

        step_idx += 1

        if grad_buffer is None:
            grad_buffer = train_entry
        else:
            for tgt_grad, grad in zip(grad_buffer, train_entry):
                tgt_grad += grad

        if step_idx % TRAIN_BATCH == 0:
            for param, grad in zip(net.parameters(), grad_buffer):
                grad_v = Variable(torch.from_numpy(grad))
                if args.cuda:
                    grad_v = grad_v.cuda()
                param.grad = grad_v

            nn_utils.clip_grad_norm(net.parameters(), CLIP_GRAD)
            optimizer.step()
            for tgt_net in tgt_nets:
                tgt_net.sync()
            grad_buffer = None