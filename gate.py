"""Deterministic safety gate for Pocket Agent. Pure rules, no I/O.

Decides which clicks are irreversible (need human approval) and renders
the approval-card text. Biased toward over-gating: when in doubt, gate.
"""
import re

SUBMIT_WORDS = re.compile(
    r"\b(submit|register|send|post|publish|buy|purchase|order|pay|transfer|withdraw|"
    r"confirm|place|sign ?up|apply|delete|remove|finish|complete|checkout|book|reserve|"
    r"schedule|cancel booking|close account|change password|reset password)\b",
    re.I,
)

HIGH_IMPACT_TYPES = {"submit"}

# Reversible controls which enter an application flow. Keep this deliberately
# narrow: a final "Submit application" control does not match.
APPLICATION_ENTRY_WORDS = re.compile(
    r"^\s*(easy\s+apply|apply|apply\s+now|continue\s+to\s+application|"
    r"apply\s+on\s+(?:company|employer)\s+(?:site|website))\s*$",
    re.I,
)

_MAX_FIELD_LINES = 24


def is_irreversible_click(el: dict) -> bool:
    """True if clicking `el` might be irreversible and needs approval.

    el keys: tag, type, text, aria_label, in_form (bool).
    Rules (bias to over-gating):
      a) type == "submit"  -> True
      b) a <button> inside a <form> is an implicit submit -> True,
         UNLESS it has an explicit type="button" (then rule (a)/(b) do not
         apply, but rule (c) still can).
      c) SUBMIT_WORDS match on text or aria-label -> True.
    """
    tag = (el.get("tag") or "").lower()
    typ = (el.get("type") or "").lower()
    text = el.get("text") or ""
    aria = el.get("aria_label") or ""

    if typ in HIGH_IMPACT_TYPES:
        return True
    if SUBMIT_WORDS.search(text) or SUBMIT_WORDS.search(aria):
        return True
    if el.get("in_form") and tag == "button" and typ != "button":
        return True
    return False


def is_application_entry_click(el: dict) -> bool:
    """True only for a reversible control that opens an application flow."""
    if el.get("in_form"):
        return False
    typ = (el.get("type") or "").lower()
    if typ in HIGH_IMPACT_TYPES:
        return False
    labels = [str(el.get(k) or "").strip() for k in ("text", "aria_label", "label")]
    return any(APPLICATION_ENTRY_WORDS.fullmatch(" ".join(label.split())) for label in labels if label)


def summarize_submission(el: dict, form_values: list[str]) -> str:
    """Human-readable approval-card body for a gated click.

    form_values: pre-formatted "field = value" strings. Important values appear first.
    """
    label = (el.get("text") or el.get("aria_label") or el.get("label") or "").strip()
    if not label:
        label = el.get("tag") or "element"
    page = el.get("page") or el.get("url") or "this page"

    lines = [f"Action: {label}", f"Site: {page}"]
    if form_values:
        important = [v for v in form_values if re.search(
            r"recipient|email|phone|amount|price|total|currency|date|time|destination|address|account",
            v, re.I)]
        ordered = important + [v for v in form_values if v not in important]
        unique: list[str] = []
        for value in ordered:
            if value not in unique:
                unique.append(value)
        lines.append("Includes:")
        for value in unique[:_MAX_FIELD_LINES]:
            lines.append(f"  {value}")
        hidden = len(unique) - _MAX_FIELD_LINES
        if hidden > 0:
            lines.append(f"  …and {hidden} more completed fields")
    return "\n".join(lines)
