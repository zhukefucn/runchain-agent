"""Compatibility imports for AgentScope's team creation tools.

AgentScope currently exports these classes from ``agentscope.app._tool`` but
not from the public ``agentscope.app`` package. Keeping the fallback here
localizes the private import until an upstream public export is available.
"""

try:
    from agentscope.app import AgentCreate, TeamCreate
except ImportError:  # AgentScope 1.0.x checked into this workspace.
    from agentscope.app._tool import AgentCreate, TeamCreate

__all__ = ["AgentCreate", "TeamCreate"]
