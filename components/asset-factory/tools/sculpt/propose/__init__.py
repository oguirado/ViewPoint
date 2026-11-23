"""Local, model-free surface stroke proposals; no geometry is mutated."""
from .api import DEFAULT_POLICY, ProposalError, mesh_digest, propose, verify_generator

__all__ = ["DEFAULT_POLICY", "ProposalError", "mesh_digest", "propose", "verify_generator"]
