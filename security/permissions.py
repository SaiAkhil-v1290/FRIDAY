"""Central permission policy. Execution and approval UI arrive in later milestones."""

from tools.registry import TOOL_POLICIES


def requires_approval(tool_name: str) -> bool:
    policy = TOOL_POLICIES.get(tool_name)
    if policy is None:
        raise ValueError(f"Unknown tool: {tool_name}")
    return policy["requires_approval"]
