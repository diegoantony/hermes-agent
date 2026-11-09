"""Job-level visibility for a recovered, acknowledged worker death."""


def record_unknown_worker_outcome(
    job: dict, *, error=None, adapters=None, loop=None
) -> bool:
    """Project only this fire's uncertain outcome, without retrying its side effects.

    A terminal ledger row alone does not retire the job claim or notify its owner.
    Fence the notice and bookkeeping together so recovery cannot overwrite a newer
    fire or duplicate a failure the worker already recorded.
    """
    from cron import scheduler
    from cron.unreachable_retry import is_retry_run

    execution = scheduler.get_execution(str(job["execution_id"]))
    if not execution or execution.get("status") != "unknown":
        return False
    claim = job.get("fire_claim")
    owner = str(claim.get("by") or "") if isinstance(claim, dict) else ""
    if not owner:
        return False
    with scheduler.fire_claim_fence(job["id"], expected_owner=owner) as owns_claim:
        if not owns_claim:
            return True
        error = error or execution["error"]
        scheduler.logger.error("Job '%s': %s", job["id"], error)
        scope_tokens = scheduler._install_fire_secret_scope()
        delivery_error = None
        try:
            delivery_error, _ = scheduler._deliver_crash_failure(
                job, error, adapters=adapters, loop=loop
            )
        finally:
            scheduler._reset_fire_secret_scope(scope_tokens)
            scheduler.mark_job_run(
                job["id"],
                False,
                error,
                delivery_error=delivery_error,
                # t_866c5d34: this is a RECOVERED UNKNOWN outcome, not a confirmed failure —
                # the worker was handed the fire and the owner lost track of it afterwards
                # (connect_gateway/ensure_gateway_runtime flaking under load, a dropped
                # websocket mid-poll, etc.); whether side effects (the agent's reply, its
                # delivery) ran is explicitly NOT known (see external_worker_exited_reason's
                # docstring above). A real case: job 'b3c2e80317eb' (FPF D60 weekly check-in,
                # 2026-11-09) was stamped last_status="error" here from a transient
                # GatewayUnavailableError, yet cron.scheduler's own log shows the SAME
                # execution delivered its reply to Telegram two seconds later — last_status
                # ="error" was simply false. Writing the dedicated "unknown" status (instead
                # of letting mark_job_run derive "error" from success=False) keeps every
                # other effect of this call identical (last_error, failure_streak,
                # last_failure, retry-ladder bookkeeping all still key off `success=False`)
                # while no longer asserting a failure nobody actually confirmed. Consumers
                # that alarm strictly on last_status == "error" (e.g. the platform
                # self-audit) stop opening false "cron em erro" cards for this class of
                # outcome; `hermes cron list` and `last_error` still surface it for anyone
                # who wants to look.
                status="unknown",
                expected_fire_owner=owner,
                # A ladder re-run's occurrence already counted toward repeat.
                **({"ladder_rung": True} if is_retry_run(job) else {}),
            )
    return True


def external_worker_exited_reason(returncode: int | None) -> str:
    """Cause for an attempt whose restart-safe worker this process watched exit.

    The generic dead-owner sweep can only say the owner vanished, so it names a
    scheduler restart — but this waiter never lost its scheduler: it handed the
    attempt to the external worker and then saw that worker exit with this status.
    The run is still ``unknown`` rather than ``failed`` (whether side effects ran is
    still unknown); only the cause becomes truthful (#128509).
    """
    return (
        f"Restart-safe cron worker exited with status {returncode} after adopting this "
        "execution, without writing a durable terminal state; whether side effects ran "
        "is unknown."
    )
