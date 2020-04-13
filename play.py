import gym
import random


env = gym.make("blokus:blokus-v0")  # Make sure to do: pip install -e blokus in root
observation = env.reset()
while True:
    env.render()
    actions = env.ai_possible_moves()
    if len(actions) == 0:
        actions = [None]

    observation, reward, done, info = env.step(random.choice(actions))
    print(observation)

    print(done)
    if done:
        observation = env.reset()
env.close()
