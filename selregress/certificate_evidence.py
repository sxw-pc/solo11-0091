"""Parse a local synthetic certificate registry snapshot without choosing a fork."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .snapshot_reader import read_snapshot
from .util import PROJECT_ROOT


class CertificateSnapshotError(ValueError):
    pass


def inspect_certificate_snapshot(path: str | Path, *, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    snapshot = read_snapshot(Path(path), site="rec.invalid", page_type="detail",
                             version="2026-01", root=root)
    main = snapshot.soup.select_one("main[data-certificate]")
    if main is None:
        raise CertificateSnapshotError("certificate header missing")
    try:
        claim_mwh = int(main["data-claim-mwh"])
        certificate = main["data-certificate"]
        claim_hour = main["data-claim-hour"]
    except (KeyError, ValueError) as exc:
        raise CertificateSnapshotError("invalid claim header") from exc
    if claim_mwh <= 0 or not certificate or not claim_hour:
        raise CertificateSnapshotError("invalid claim header")
    events: dict[str, dict[str, Any]] = {}
    outputs = set()
    for row in snapshot.soup.select("#certificate-events tr[data-event]"):
        event_id = row.get("data-event")
        source = row.get("data-input")
        authorizer = row.get("data-authorizer")
        output = row.get("data-output")
        owner = row.get("data-owner")
        try:
            units = int(row["data-units"])
        except (KeyError, ValueError) as exc:
            raise CertificateSnapshotError("invalid event units") from exc
        if (not event_id or source is None or not authorizer or not output or not owner or units <= 0
                or output in outputs):
            raise CertificateSnapshotError("malformed or duplicate event output")
        outputs.add(output)
        event = events.setdefault(event_id, {"id": event_id, "input": source,
                                             "authorizer": authorizer,
                                             "outputs": [], "hour": row.get("data-hour")})
        if (event["input"] != source or event["authorizer"] != authorizer
                or event["hour"] != row.get("data-hour")):
            raise CertificateSnapshotError("event output rows disagree")
        event["outputs"].append({"id": output, "owner": owner, "units": units})
    if not events:
        raise CertificateSnapshotError("no certificate events")
    witnesses: dict[str, set[str]] = {event: set() for event in events}
    for row in snapshot.soup.select("#mirror-witnesses tr[data-event]"):
        event_id, mirror = row.get("data-event"), row.get("data-mirror")
        if event_id not in witnesses or mirror not in {"east", "west", "central"}:
            raise CertificateSnapshotError("unknown mirror witness")
        if mirror in witnesses[event_id]:
            raise CertificateSnapshotError("duplicate mirror witness")
        witnesses[event_id].add(mirror)
    spenders: dict[str, list[str]] = {}
    for event in events.values():
        if event["input"]:
            spenders.setdefault(event["input"], []).append(event["id"])
    return {
        "snapshot_path": snapshot.meta.path,
        "snapshot_sha256": snapshot.meta.sha256,
        "certificate": certificate,
        "claim_hour": claim_hour,
        "claim_mwh": claim_mwh,
        "events": list(events.values()),
        "witnesses": {key: sorted(value) for key, value in witnesses.items()},
        "competing_spends": {key: sorted(value) for key, value in spenders.items() if len(value) > 1},
        "strict_no_fork_decision": None,
        "quorum_witness_decision": None,
    }
