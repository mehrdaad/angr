from . import ExplorationTechnique
from .. import sim_options

import logging
l = logging.getLogger("angr.exploration_techniques.symbion")

class Symbion(ExplorationTechnique):
    """
     The Symbion exploration technique uses only the SimEngineConcrete available in order
     to step a SimState.
     :param find: list of addresses that we want to reach, this will be translated in setting a breakpoint
                  inside the concrete process using the ConcreteTarget interface provided by the user.
    """
    def __init__(self, find=None, find_stash='found'):
        super(Symbion, self).__init__()
        self.find = find
        self.find_stash = find_stash

    def setup(self, simgr):
        if not self.find_stash in simgr.stashes: simgr.stashes[self.find_stash] = []

    def step(self, simgr, stash, **kwargs):
        # check if the stash contains only one SimState and if not warn the user that only the first state
        # in the stash can be stepped in the SimEngineConcrete.
        # This because for now we support only one concrete execution, in future we can think about a snapshot
        # engine and give to each SimState an instance of a concrete process.
        if len(simgr.stashes[stash]):
            l.warning(self, "You are trying to use the Symbion exploration technique on multiple state, "
                            "this is not supported now.")


        pass

    def filter(self, state):
        # check condition on the state that we need to step inside the SimEngineConcrete,
        # like is the SimState concretizable?
        l.warning(self, "Checking if the state is concretizable before entering into the concrete world!")
        return state.se.eval('everything')

        pass

    def step_state(self, state, **kwargs):
        """
        This function will force the step of the state
        inside an instance of a SimConcreteEngine.
        :param state: the state to step inside the SimConcreteEngine
        :param kwargs:
        :return:
        """
        ss = self.project.factory.successors(state, engine=self.project.concrete_engine, break_address=self.find)
        return ss

    def complete(self, simgr):
        pass

