"""Read-only, administrator-only views; never trigger upstream requests."""
from __future__ import annotations
from datetime import datetime, timezone
import time


def config_view(policy):
    return {
        "config_revision":policy.revision,"authority":"versioned_file","editable":False,
        "profile":policy.profile,"alias":policy.alias,"privacy_scope":policy.privacy_scope,
        "limits":policy.limits.model_dump(),
        "candidates":[{"id":d.id,"provider":d.provider,"model":d.model,"order":d.order,
            "enabled":d.enabled,"adapter":d.adapter,"eligibility":d.eligibility.status,
            "eligibility_evidence":d.eligibility.evidence,
            "checked_at":d.eligibility.checked_at.isoformat() if d.eligibility.checked_at else None,
            "billing_hard_limit_reviewed":d.eligibility.billing_hard_limit,
            "review_by":d.eligibility.review_by.isoformat() if d.eligibility.review_by else None,
            "privacy_scopes":d.eligibility.privacy_scopes,"capability":d.capability.model_dump(),
            "pool_ids":d.quota_pools,"exclusion":policy.exclusion(d)}
            for d in sorted(policy.deployments,key=lambda x:x.order)],
        "preview_is_execution_guarantee":False,
    }


async def resources_view(policy,state):
    await state.ready()
    now=datetime.now(timezone.utc)
    pools=[]
    for pool in policy.pools:
        observation=state.quota_snapshot(pool.id,now).to_dict()
        value=state.pools.get(pool.id,{})
        next_probe=value.get("next_probe_at",0)
        if pool.id in state.disabled:
            availability="disabled"
        elif next_probe>now.timestamp():
            availability="known_exhausted" if value.get("status")=="known_exhausted" else "cooldown"
        elif pool.id in state.inflight and state._pool_unknown(pool.id,now.timestamp()):
            availability="probing"
        elif state._pool_unknown(pool.id,now.timestamp()):
            availability="unknown"
        else:
            availability="available"
        pools.append({"id":pool.id,"provider":pool.provider,"account_scope":pool.account_scope,
            "model_scope":pool.model_scope,"dimension":pool.dimension,"window":pool.window,
            "reset_timezone":pool.reset_timezone,"scope_reference":pool.shared_scope_evidence,
            "availability":availability,"next_probe_at":datetime.fromtimestamp(next_probe,timezone.utc).isoformat() if next_probe>now.timestamp() else None,
            "next_probe_source":value.get("next_probe_source", "retry_after_or_cooldown_policy" if next_probe>now.timestamp() else None),
            "next_probe_is_recovery_guarantee":False,"observation":observation})
    candidates=[]
    for d in sorted(policy.deployments,key=lambda x:x.order):
        reason=policy.exclusion(d)
        if reason is None and not await state.check_pools(d.quota_pools):
            reason="quota_or_cooldown_unavailable"
        candidates.append({"id":d.id,"order":d.order,"provider":d.provider,"model":d.model,
            "free_eligibility":d.eligibility.status,"enabled":d.enabled,
            "review_by":d.eligibility.review_by.isoformat() if d.eligibility.review_by else None,
            "eligible_for_reviewed_scope":reason is None,
            "exclusion":reason,"pool_ids":d.quota_pools})
    return {"config_revision":policy.revision,"observed_at":now.isoformat(),
        "refresh_causes_inference":False,"preview_is_execution_guarantee":False,
        "account_billing_zero_confirmed":None,"candidates":candidates,"pools":pools}
