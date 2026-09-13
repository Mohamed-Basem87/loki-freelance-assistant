"""Notification application service with broadcast semantics."""
import inspect

from app.adapters.notifications.registry import build as build_sinks


class NotificationService:
    """Broadcast each notification to every configured sink.

    A send is successful only when every configured sink succeeds. Every sink
    is attempted independently, so one broken sink cannot prevent healthy
    sinks from receiving the notification.
    """

    def __init__(self, sinks=None, repository=None):
        self.sinks = tuple(sinks if sinks is not None else build_sinks())
        self.repository = repository

    async def send(self, **payload):
        if not self.sinks:
            return False

        job_uuid = payload.get("job_uuid", "")
        current_status = ""
        if self.repository is not None and job_uuid:
            # Do not swallow this read failure and default to "no
            # sink has succeeded yet": that assumption is exactly
            # backwards if some sinks already have succeeded, and
            # would send a real duplicate notification to them. An
            # unreadable prior state is treated the same as an
            # unwritable one below -- surfaced, not guessed at.
            value = self.repository.get_job(job_uuid)
            row = await value if inspect.isawaitable(value) else value
            current_status = (row or {}).get("Notification Status") or ""

        def sink_state(sink_id):
            marker = f"Sink:{sink_id}="
            for part in current_status.split(";"):
                part = part.strip()
                if part.startswith(marker):
                    return part[len(marker):]
            return ""

        def with_sink_state(sink_id, state):
            nonlocal current_status
            parts = [p.strip() for p in current_status.split(";") if p.strip()]
            marker = f"Sink:{sink_id}="
            parts = [p for p in parts if not p.startswith(marker)]
            parts.append(f"{marker}{state}")
            current_status = "; ".join(parts)

        results = []
        seen_sink_ids = set()
        for index, sink in enumerate(self.sinks, start=1):
            sink_id = str(getattr(sink, "id", sink.__class__.__name__)).strip().lower()
            if sink_id in seen_sink_ids:
                sink_id = f"{sink_id}-{index}"
            seen_sink_ids.add(sink_id)
            if sink_state(sink_id) == "Sent":
                results.append(True)
                continue
            try:
                ok = bool(await sink.send(**payload))
            except Exception as exc:
                ok = False
                if self.repository is not None:
                    try:
                        value = self.repository.log_error("Notifier", exc, job_uuid, save=False)
                        if inspect.isawaitable(value):
                            await value
                    except Exception as log_exc:
                        # Logging the original send failure is
                        # best-effort only: `ok = False` is already
                        # recorded below regardless of whether this
                        # succeeds, so a broken logger here must not
                        # stop that from happening. Still surface it
                        # rather than hide it entirely.
                        print(
                            f"[NOTIFIER] failed to log sink error for "
                            f"job {job_uuid!r}, sink {sink_id!r}: "
                            f"{log_exc!r} (original send error: {exc!r})"
                        )
            with_sink_state(sink_id, "Sent" if ok else "Failed")
            if self.repository is not None and job_uuid:
                try:
                    value = self.repository.update_job(job_uuid, notification_status=current_status, save=True)
                    if inspect.isawaitable(value):
                        value = await value
                    if value is False:
                        # update_job() returns False (rather than
                        # raising) when it can't find/persist the row
                        # -- e.g. the job disappeared between send()
                        # starting and this write. That is exactly as
                        # dangerous as an exception here: the sink
                        # state we just computed never actually made
                        # it to durable storage, so treat it the same
                        # way (see the comment below on why this
                        # raises instead of swallowing the failure).
                        raise RuntimeError(
                            f"update_job returned False for job "
                            f"{job_uuid!r}, sink {sink_id!r} "
                            f"(send succeeded={ok})"
                        )
                except Exception as exc:
                    # This write is the durable record of whether
                    # `sink_id` has already been notified for this
                    # job. Silently swallowing its failure was the
                    # audit finding: if `ok` is True here (the sink
                    # really did send) but this persist call fails,
                    # a later retry sweep would see this sink as
                    # still unresolved and resend to it -- a real
                    # delivery-duplication risk, not a cosmetic
                    # logging gap. Never falsely report durable
                    # success: raise instead, so this send() call
                    # aborts rather than continuing to attempt
                    # further sinks while the state we'd base that on
                    # is unknown to be wrong. Nothing already
                    # persisted is lost -- "Notification Status" is
                    # left exactly as it was before this attempt, so
                    # the retry sweep (which already wraps every job
                    # in its own try/except and keeps going) picks
                    # the whole job back up and retries it in full.
                    raise RuntimeError(
                        f"failed to persist notification state for job "
                        f"{job_uuid!r}, sink {sink_id!r} "
                        f"(send succeeded={ok}): {exc}"
                    ) from exc
            results.append(ok)
        return all(results)



_notification_service = None


def get_notification_service():
    global _notification_service
    if _notification_service is None:
        _notification_service = NotificationService()
    return _notification_service


async def send_notification(**payload):
    return await get_notification_service().send(**payload)
