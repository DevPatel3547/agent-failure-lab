"""A stateless, contract-aware deterministic baseline, not a model evaluation."""

from .schema import Action, digest


class ReferenceAgent:
    provider = "scripted"
    model = None
    policy_name = "contract-aware-reference-v1"

    def decide(self, context: dict, mode: str) -> Action:
        history = context["history"]
        contract = context["contract"]
        last = history[-1].get("observation", {}) if history else {}
        status = last.get("status")

        def finish(status, reason, ticket_id=None):
            return Action("finish", {"status": status, "ticket_id": ticket_id, "reason": reason})

        if status in {"created", "found"}:
            if len(set(last.get("ticket_ids", []))) > 1:
                return finish(
                    "unknown", "Multiple committed tickets were observed; the task is not confirmed correct."
                )
            return finish(
                "succeeded",
                "The provider supplied a matching acknowledgement or visible record.",
                last.get("ticket_id"),
            )
        ambiguous = any(
            item.get("observation", {}).get("status")
            in {"uncertain", "pending", "worker_restarted", "conflict"}
            for item in history
        )
        if status == "rejected":
            return finish(
                "unknown" if ambiguous else "failed",
                "A rejected request cannot resolve an earlier uncertain write."
                if ambiguous
                else "The initial request was definitely rejected.",
            )
        if status == "conflict":
            return finish("unknown", "Conflicting effects require reconciliation.")
        if context["remaining_steps"] <= 1:
            return finish("unknown", "Recovery budget exhausted without sufficient evidence.")
        # Observe the complete Retry-After interval even after intermediate wait responses.
        for index in range(len(history) - 1, -1, -1):
            observation = history[index].get("observation", {})
            if observation.get("status") != "waited":
                if observation.get("status") == "rate_limited":
                    elapsed = sum(
                        item.get("observation", {}).get("status") == "waited" for item in history[index + 1 :]
                    )
                    if elapsed < observation.get("retry_after", 0):
                        return Action("wait", {})
                break
        can_retry = contract["supports_idempotency"] and contract.get("key_ttl") is None
        if ambiguous:
            if contract["supports_lookup"] and status not in {"not_found", "unavailable"}:
                return Action("lookup_ticket", {})
            if not can_retry:
                if contract["supports_lookup"]:
                    return Action("lookup_ticket", {})
                return finish(
                    "unknown", "Cannot safely replay an uncertain write under this provider contract."
                )
            if status == "pending":
                return Action("wait", {})
        key = "reference-" + digest(context["task"])[:40] if contract["supports_idempotency"] else None
        return Action("create_ticket", {"title": context["task"]["title"], "idempotency_key": key})
