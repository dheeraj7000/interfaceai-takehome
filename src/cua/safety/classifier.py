"""
Action Risk Classifier — determines whether an action is safe, moderate, or risky.

Classification logic:
- Safe: read-only operations (navigate, extract, assert, screenshot, wait, scroll)
- Moderate: writes that are typically reversible (type, clear, select, hover)
- Risky: actions that trigger irreversible side effects (click on submit/confirm/delete,
  form submissions, transfers, etc.)

The classifier uses both the action type and contextual signals (button text,
form action, URL path) to make its determination. This is intentionally
conservative — it's better to flag a safe action as moderate than to miss
a risky one.

In the banking context, "risky" means: could move money, change account state,
delete records, or perform any operation that can't be undone by the system.
"""

from __future__ import annotations

import re

from cua.schema.artifact import ActionType, Locator, RiskLevel


# Patterns that suggest a risky/irreversible operation
RISKY_TEXT_PATTERNS = [
    r"\bconfirm\b",
    r"\bexecute\b",
    r"\bsubmit\b",
    r"\bdelete\b",
    r"\bremove\b",
    r"\btransfer\b",
    r"\bapprove\b",
    r"\bauthorize\b",
    r"\bclose\s+account\b",
    r"\bfinal\b",
    r"\birreversible\b",
    r"\bpermanent\b",
]

RISKY_URL_PATTERNS = [
    r"/transfer/execute",
    r"/delete",
    r"/close",
    r"/approve",
    r"/submit",
    r"/confirm",
]

# Actions that are inherently read-only
SAFE_ACTIONS = {
    ActionType.NAVIGATE,
    ActionType.EXTRACT,
    ActionType.ASSERT,
    ActionType.SCREENSHOT,
    ActionType.WAIT,
    ActionType.SCROLL,
}

# Actions that modify state but are usually reversible
MODERATE_ACTIONS = {
    ActionType.TYPE,
    ActionType.CLEAR,
    ActionType.SELECT,
    ActionType.HOVER,
    ActionType.PRESS_KEY,
}


def classify_action(
    action: ActionType,
    target: Locator | None = None,
    value: str | None = None,
    current_url: str = "",
) -> RiskLevel:
    """Classify the risk level of an action.

    Args:
        action: The action type
        target: The target element locator (if any)
        value: The action value (text to type, URL to navigate to, etc.)
        current_url: The current page URL

    Returns:
        RiskLevel indicating how risky this action is
    """
    # Inherently safe actions
    if action in SAFE_ACTIONS:
        # Navigate can be risky if it's submitting to a risky URL
        if action == ActionType.NAVIGATE and value:
            for pattern in RISKY_URL_PATTERNS:
                if re.search(pattern, value, re.IGNORECASE):
                    return RiskLevel.RISKY
        return RiskLevel.SAFE

    # Check current URL for risky context
    if current_url:
        for pattern in RISKY_URL_PATTERNS:
            if re.search(pattern, current_url, re.IGNORECASE):
                # Any click on a risky page is risky
                if action == ActionType.CLICK:
                    return RiskLevel.RISKY

    # Click actions — check the target for risky signals
    if action == ActionType.CLICK and target:
        text_to_check = " ".join(filter(None, [
            target.semantic,
            target.value,
            target.a11y_name,
        ]))
        for pattern in RISKY_TEXT_PATTERNS:
            if re.search(pattern, text_to_check, re.IGNORECASE):
                return RiskLevel.RISKY

    # Moderate actions
    if action in MODERATE_ACTIONS:
        return RiskLevel.MODERATE

    # Click with no risky signals — moderate (it has side effects but
    # we couldn't identify it as specifically risky)
    if action == ActionType.CLICK:
        return RiskLevel.MODERATE

    # Default to safe
    return RiskLevel.SAFE
