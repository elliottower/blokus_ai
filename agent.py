import os

import gym
import torch.optim as optim

from models import *
from memory_replay import ReplayMemory
from blokus.envs.blokus_env import BlokusEnv


class Agent:
    """
    :param env: gym environment
    :param memory_size: maximum capacity of the memory replay
    :param batch_size
    :param learning_rate
    :param num_episodes: number of games played to train on
    :param eps: initial epsilon value in the epsilon-greedy policy
    :param min_eps: minimal possible value of epsilon in the epsilon-greedy policy
    :param eps_decay: decrease rate of epsilon in the epsilon-greedy policy
    :param gamma: discount factor used to measure the target
    :param is_double: boolean to have double DQN network
    :param is_dueling: boolean to have dueling network
    :param is_noisy: boolean to have a noisy network
    :param is_distributional: boolean to have a distributional network
    :param dist_params: dictionary containing parameters for distributional network including num_bins (number of bins
                        the distribution return), v_min (minimal state value), v_max (maximal state value)
                        e.i: {num_bin:51, v_min:0, v_max:1} 51 atoms are used in the paper
    """

    def __init__(self,
                 env,
                 memory_size,
                 batch_size,
                 learning_rate,
                 num_episodes,
                 model_filename,
                 eps=1,
                 min_eps=0.01,
                 eps_decay=0.005,
                 gamma=0.9,
                 is_double=False,
                 is_dueling=False,
                 is_noisy=False,
                 is_distributional=False,
                 distr_params=None):
        self.env = env
        self.num_episodes = num_episodes
        self.gamma = gamma
        self.batch_size = batch_size
        self.memory = ReplayMemory(memory_size, self.batch_size)
        self.eps = eps
        self.min_eps = min_eps
        self.eps_decay = eps_decay
        self.device = torch.device("cuda:" + str(0) if torch.cuda.is_available() else "cpu")
        self.model_path = os.path.join("models", model_filename + ".pt")
        # Blokus
        # self.obs_size = env.observation_space.shape[0] * env.observation_space.shape[1]
        self.obs_size = env.observation_space.n
        self.is_dueling = is_dueling
        self.is_noisy = is_noisy
        self.is_distributional = is_distributional
        if self.is_distributional:
            self.distr_params = distr_params
            self.distr_params["v_range"] = torch.linspace(self.distr_params["v_min"],
                                                          self.distr_params["v_max"],
                                                          self.distr_params["num_bins"]).to(self.device)
        if self.is_dueling:
            self.model = DuelingNetwork(self.obs_size, env.action_space.n).to(self.device)
        elif self.is_noisy:
            self.model = NoisyNetwork(self.obs_size, env.action_space.n).to(self.device)
        elif self.is_distributional:
            self.model = DistributionalNetwork(self.obs_size, env.action_space.n, self.distr_params).to(self.device)
        else:
            self.model = DQN(self.obs_size, env.action_space.n).to(self.device)
        # self.model = torch.load(self.model_path, map_location=self.device)
        self.is_double = is_double
        self.loss = []
        if self.is_double:
            self.model_target = DQN(self.obs_size, env.action_space.n).to(self.device)
            self.model_target.load_state_dict(self.model.state_dict())
            self.model_target.eval()
        self.optimizer = optim.Adam(self.model.parameters(), lr=learning_rate)

    def eps_greedy_action(self, state):
        # Explore the environment
        if self.eps > np.random.random():
            # Take a random action
            next_action = self.env.action_space.sample()
        # Greedy choice (exploitation)
        else:
            next_action = int(self.model(state, self.env).argmax().detach().cpu())

        return next_action

    def update(self, reward, done, next_state, state, action):
        if self.is_distributional:
            loss = self.get_distributional_loss(reward, done, next_state, state, action)
        else:
            target = self.get_target(reward, done, next_state)
            prediction = self.model(state, self.env)[action]
            loss = F.smooth_l1_loss(prediction, target)
        self.loss.append(loss)
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        if self.is_noisy:
            self.model.update_noise()

    def get_distributional_loss(self, reward, done, next_state, state, action):
        action_distr = self.model.action_distr(state)
        log_action_distr = action_distr[:batch_size, action].log()

        reward = torch.tensor(reward).type(torch.float32).to(self.device)
        d_target = reward.clamp(self.distr_params["v_min"], self.distr_params["v_max"])
        if done:
            d_target = (self.gamma * self.distr_params["v_range"] + reward).clamp(self.distr_params["v_min"],
                                                                                  self.distr_params["v_max"])
        v_step = (self.distr_params["v_max"] - self.distr_params["v_min"]) / (self.distr_params["num_bins"] - 1)
        delta = (d_target - self.distr_params["v_min"]) / v_step

        offset = torch.linspace(0,
                                (self.batch_size - 1) * self.distr_params["num_bins"],
                                self.batch_size).to(self.device)
        next_action = self.model(next_state).argmax()
        next_action_distr = self.model.action_distr(next_state)[next_action, :self.batch_size]

        # Projection
        distr_projection = torch.zeros(next_action_distr.shape).to(self.device)
        distr_projection.reshape(-1).index_add_(0,
                                                (delta.floor() + offset).long(),
                                                next_action_distr * (delta.ceil() - delta))
        distr_projection.reshape(-1).index_add_(0,
                                                (delta.ceil() + offset).long(),
                                                next_action_distr * (delta - delta.floor()))
        return - (distr_projection * log_action_distr).sum(-1).mean()

    def get_target_double(self, next_state):
        action = self.model(next_state, self.env).argmax()
        return self.model_target(next_state)[action]

    def get_target(self, reward, done, next_state):
        # y = r if done
        target = torch.tensor(reward).type(torch.float32).to(self.device)
        if not done:
            # y = r + gamma * max Q(s',a') if not done
            if self.is_double:
                next_state_max_Q = self.get_target_double(next_state)
            else:
                next_state_max_Q = self.model(next_state, self.env).max()
            target = next_state_max_Q * self.gamma + reward
        return target

    def replay(self):
        batch = self.memory.random_batch()
        for state, action, next_state, reward, done in batch:
            target = self.get_target(reward, done, next_state)
            self.update(state, target, action)

    def ohe(self, state):
        ohe_state = torch.zeros(self.obs_size).to(self.device)
        ohe_state[state] = 1
        return ohe_state

    # def ohe(self, state):
    #     return state.view(-1).type(torch.float32).to(self.device)

    def train(self):
        rewards_lst = []
        best_rate = 0
        for i in range(self.num_episodes):
            rewards = 0
            done = False
            state = self.ohe(self.env.reset())
            while not done:
                action = self.eps_greedy_action(state)
                next_state, reward, done, info = self.env.step(action)
                # env.render("minmal")
                rewards += reward
                next_state = self.ohe(next_state)
                self.memory.add_to_memory(state, action, next_state, reward, done)

                self.update(reward, done, next_state, state, action)
                self.eps = self.min_eps + (self.eps - self.min_eps) * np.exp(-self.eps_decay * i)

                rewards_lst.append(rewards)
                state = next_state

                if not i % 20 and self.is_double:
                    self.model_target.load_state_dict(self.model.state_dict())

            if len(self.memory) > self.batch_size:
                self.replay()

            if not i % 10 and i != 0:
                print('Episode {} Loss: {} Reward Rate {}'.format(i, self.loss[-1], str(sum(rewards_lst) / i)))
                if (sum(rewards_lst) / i) > best_rate:
                    best_rate = (sum(rewards_lst) / i)
                    torch.save(self.model, self.model_path)
        torch.save(self.model, self.model_path)
        self.env.close()

    def test(self):
        self.model = torch.load(self.model_path, map_location=self.device)
        done = False
        state = self.ohe(self.env.reset())
        rewards = 0
        self.eps = self.min_eps
        while not done:
            action = self.eps_greedy_action(state)
            # self.env.render()
            next_state, reward, done, info = self.env.step(action)
            rewards += reward
            state = self.ohe(next_state)
        if rewards:
            print("Victory")
        else:
            print("Lost")
        self.env.close()


if __name__ == "__main__":
    env = gym.make("FrozenLake-v0")
    # env = gym.make("blokus:blokus-v0")
    memory_size = 1000
    num_episodes = 4000
    batch_size = 32
    # gamma = 0.999
    learning_rate = 0.001
    model_filename = "noisy_frozen_lake"

    dist_params = {"num_bins":51, "v_min":0, "v_max":1}
    agent = Agent(env, memory_size, batch_size, learning_rate, num_episodes, model_filename, is_double=False,
                  is_dueling=False, is_noisy=False, is_distributional=True, distr_params=dist_params)
    agent.train()
    # for i in range(10):
    #     agent.test()

