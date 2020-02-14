"""Sampling kernels.

.. note:
    This file is a "flat zone": positions and logprobs are 1 dimensional
    arrays. Raveling and unraveling logic must happen outside.
"""
from functools import partial
from typing import Callable, NamedTuple, Tuple

import jax
import jax.numpy as np
import jax.numpy.DeviceArray as Array

__all__ = ["hmc_kernel", "rwm_kernel"]


class HMCState(NamedTuple):
    """Describes the state of the HMC kernel.

    It only contains the minimum information necessary to move the
    chain forward.
    """

    position: Array
    log_prob: float
    log_prob_grad: float
    energy: float


class HMCInfo(NamedTuple):
    """Additional information on the current HMC step.
    """

    proposed_state: HMCState
    acceptance_probability: float
    is_accepted: bool
    is_divergent: bool


def hmc_kernel(
    integrator: Callable,
    momentum_generator: Callable,
    kinetic_energy: Callable,
    divergence_threshold: float = 1000.0,
) -> Callable:
    """Creates a Hamiltonian Monte Carlo transition kernel.

    Hamiltonian Monte Carlo is known to yield effective Markov transitions and
    has been a major empirical success, leading to an extensive use in
    probabilistic programming languages and libraries [Duane1987, Neal1994,
    Betancourt2018]_.

    It works by augmenting the state space in which the chain evolves with an
    auxiliary momentum :math:`p`. At each step of the chain we draw a momentum
    value from the `momentum_generator` function. We then use Hamilton's
    equations [HamiltonEq]_ to push the state forward; we then compute the new
    state's energy using the `kinetic_energy` function and `logpdf` (potential
    energy). While the hamiltonian dynamics is conservative, numerically
    integration can introduce some discrepancy; we perform a metropolis
    acceptance test to compensate for integration errors after having flipped
    the new state's momentum to make the transition reversible.

    I encourage anyone interested in the theoretical underpinning of the method
    to read Michael Betancourts' excellent introduction [Betancourt2018]_.

    This implementation is very general and should accomodate most variations
    of the method.

    Arguments
    ---------
    logpdf:
        The logpdf of the model whose posterior we want to sample. Returns the
        log probability and gradient when evaluated at a position.
    integrator:
        The function used to integrate the equations of motion.
    momentum_generator:
        A function that returns a new value for the momentum when called.
    kinetic_energy:
        A function that computes the trajectory's kinetic energy.
    divergence_threshold:
        The maximum difference in energy between the initial and final state
        after which we consider the transition to be divergent.

    Returns
    -------
    A kernel that moves the chain by one step.

    References
    ----------
    .. [Duane1987]: Duane, Simon, et al. "Hybrid monte carlo." Physics letters B
                    195.2 (1987): 216-222.
    .. [Neal1994]: Neal, Radford M. "An improved acceptance procedure for the
                   hybrid Monte Carlo algorithm." Journal of Computational Physics 111.1
                   (1994): 194-203.
    .. [Betancourt2018]: Betancourt, Michael. "A conceptual introduction to
                         Hamiltonian Monte Carlo." arXiv preprint arXiv:1701.02434 (2018).
    .. [HamiltonEq]: "Hamiltonian Mechanics", Wikipedia.
                     https://en.wikipedia.org/wiki/Hamiltonian_mechanics#Deriving_Hamilton's_equations
    """

    @jax.jit
    def kernel(rng_key, state: HMCState) -> Tuple[HMCState, HMCInfo]:
        """Moves the chain by one step using the Hamiltonian dynamics.

        Arguments
        ---------
        rng_key:
           The pseudo-random number generator key used to generate random numbers.
        state:
            The current state of the chain: position, log-probability and gradient
            of the log-probability.

        Returns
        -------
        Next state of the chain and information about the current step.
        """
        key_momentum, key_integrator, key_accept = jax.random.split(rng_key, 3)

        position, log_prob, log_prob_grad, energy = state
        momentum = momentum_generator(key_momentum)
        position, momentum, log_prob, log_prob_grad = integrator(
            key_integrator, position, momentum, log_prob_grad, log_prob,
        )

        flipped_momentum = -1.0 * momentum  # to make the transition reversible
        new_energy = log_prob + kinetic_energy(flipped_momentum)
        new_state = HMCState(position, log_prob, log_prob_grad, energy)

        delta_energy = energy - new_energy
        delta_energy = np.where(np.isnan(delta_energy), -np.inf, delta_energy)
        is_divergent = np.abs(delta_energy) > divergence_threshold
        p_accept = np.clip(np.exp(delta_energy), a_max=1)

        do_accept = jax.random.bernoulli(key_accept, p_accept)
        accept_state = (new_state, HMCInfo(new_state, p_accept, True, is_divergent))
        reject_state = (state, HMCInfo(new_state, p_accept, False, is_divergent))
        return np.where(do_accept, accept_state, reject_state)

    return kernel


#
# Random Walk Metropolis
#


class RWMState(NamedTuple):
    position: Array
    log_prob: float


class RMWInfo(NamedTuple):
    is_accepted: bool
    proposed_state: RWMState


@partial(jax.jit, static_argnums=(1, 2))
def rwm_kernel(
    rng_key: jax.random.PRNGKey, logpdf: Callable, proposal: Callable, state: RWMState
) -> RWMState:
    """Random Walk Metropolis transition kernel.

    Moves the chain by one step using the Random Walk Metropolis algorithm.

    Arguments
    ---------
    rng_key: jax.random.PRNGKey
        Key for the pseudo random number generator.
    logpdf: function
        Returns the log-probability of the model given a position.
    proposal: function
        Returns a move proposal.
    state: RWMState
        The current state of the markov chain.

    Returns
    -------
    RMWState
        The new state of the markov chain.
    """
    key_move, key_uniform = jax.random.split(rng_key)

    position, log_prob = state

    move_proposal = proposal(key_move)
    proposal = position + move_proposal
    proposal_log_prob = logpdf(proposal)

    log_uniform = np.log(jax.random.uniform(key_uniform))
    do_accept = log_uniform < proposal_log_prob - log_prob

    position = np.where(do_accept, proposal, position)
    log_prob = np.where(do_accept, proposal_log_prob, log_prob)
    return RWMState(position, log_prob)
