# the GTFS-RT pollers: trip updates, vehicle positions, alerts
# Split from app.py; each module owns one part of the API and is
# wired together in app.py, which owns the FastAPI object itself.
import asyncio
import time

import httpx
from google.transit import gtfs_realtime_pb2

from regions import (REGIONS, STATE, ALERT_POLL_SECONDS, POLL_SECONDS,
                     region_cfg)




async def _fetch_feeds(client, cfg: dict, kind: str) -> tuple[list, list]:
    """Fetch every configured GTFS-RT feed of one kind for a region. Returns
    ((prefix, FeedMessage) pairs, error strings); the prefix maps feed-local
    ids onto the prefixed ids the region's ingest wrote (empty for
    single-feed regions). One failing feed must not sink the others — Sydney
    polls seven per kind, and an all-or-nothing cycle turned one flaky
    light-rail endpoint into a fully scheduled region."""
    out, errors = [], []
    gap = cfg.get("req_gap_s") or 0
    for i, feed_cfg in enumerate(cfg[kind]):
        try:
            if gap and i:
                await asyncio.sleep(gap)
            resp = await client.get(feed_cfg["url"], headers=cfg["headers"])
            resp.raise_for_status()
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(resp.content)
            out.append((feed_cfg["prefix"], feed))
        except Exception as exc:
            errors.append(f"{feed_cfg['prefix'] or '(no prefix)'} "
                          f"{feed_cfg['url']}: {exc}")
    return out, errors


async def poll_trip_updates(rid: str) -> None:
    cfg, st = REGIONS[rid], STATE[rid]
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                cache: dict = {}
                n_updates = n_no_trip = 0
                feeds, errors = await _fetch_feeds(client, cfg, "trip_updates")
                for prefix, feed in feeds:
                    for entity in feed.entity:
                        if not entity.HasField("trip_update"):
                            continue
                        tu = entity.trip_update
                        trip_id = tu.trip.trip_id
                        n_updates += 1
                        if not trip_id:
                            n_no_trip += 1
                        stops: dict = {}
                        # (stop_sequence, delay) pairs for spec-correct delay
                        # propagation: an update applies to every later stop
                        # until the next update. SEQ and Melbourne trains
                        # enumerate all remaining stops so the exact lookup
                        # suffices, but Melbourne trams/buses publish only the
                        # next stop or two — without propagation every later
                        # stop on the run showed as scheduled.
                        seqs: list = []
                        for stu in tu.stop_time_update:
                            rec: dict = {"arrival": None, "delay": None}
                            ev = None
                            if stu.HasField("arrival"):
                                ev = stu.arrival
                            elif stu.HasField("departure"):
                                ev = stu.departure
                            if ev is not None:
                                if ev.HasField("time") and ev.time:
                                    rec["arrival"] = ev.time
                                if ev.HasField("delay"):
                                    rec["delay"] = ev.delay
                            rec["skipped"] = (
                                stu.schedule_relationship
                                == stu.ScheduleRelationship.SKIPPED
                            )
                            stops[prefix + stu.stop_id] = rec
                            if stu.HasField("stop_sequence") and rec["delay"] is not None:
                                seqs.append((stu.stop_sequence, rec["delay"]))
                        seqs.sort()
                        cache[prefix + trip_id] = {"stops": stops, "seq": seqs}
                # All feeds down: keep the previous cache (stale beats empty,
                # and rt_fetch's growing age shows the staleness); partial
                # results commit — trips on a dead feed drop to scheduled.
                if feeds:
                    st["rt"] = cache
                    st["rt_fetch"] = time.time()
                st["rt_stats"] = {"trip_updates": n_updates,
                                  "without_trip_id": n_no_trip,
                                  **({"errors": errors} if errors else {})}
                print(f"[tu:{rid}] {n_updates} trip updates ({len(cache)} trips cached)"
                      + (f", {n_no_trip} without trip_id" if n_no_trip else ""))
                if errors:
                    print(f"[poll:{rid}] trip-update feed(s) failed: "
                          + " | ".join(errors))
            except Exception as exc:  # keep serving scheduled times on failure
                print(f"[poll:{rid}] realtime fetch failed: {exc}")
            await asyncio.sleep(POLL_SECONDS)


async def poll_vehicle_positions(rid: str) -> None:
    """Live GPS for the map view. Keyed by trip_id so a departure on the board
    can be matched to the vehicle actually running it."""
    cfg, st = REGIONS[rid], STATE[rid]
    # Offset from the trip-updates poller so the two bursts never align —
    # at boot all pollers fired at once, and TfNSW 429'd the tail of the
    # ~18 near-simultaneous requests.
    await asyncio.sleep(POLL_SECONDS / 3)
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                cache: dict = {}
                n_total = n_pos = n_no_pos = n_no_trip = 0
                feeds, errors = await _fetch_feeds(client, cfg, "vehicle_positions")
                for prefix, feed in feeds:
                    for entity in feed.entity:
                        if not entity.HasField("vehicle"):
                            continue
                        v = entity.vehicle
                        n_total += 1
                        # Count the drops rather than skip them silently — this
                        # is "are we losing live vehicles?", answered.
                        if not v.trip.trip_id:
                            n_no_trip += 1
                            continue
                        if not v.HasField("position"):
                            n_no_pos += 1
                            continue
                        n_pos += 1
                        pos = v.position
                        cache[prefix + v.trip.trip_id] = {
                            "lat": pos.latitude,
                            "lon": pos.longitude,
                            "bearing": pos.bearing if pos.HasField("bearing") else None,
                            "status": v.current_status,
                            "timestamp": v.timestamp or None,
                        }
                if feeds:
                    st["vp"] = cache
                    st["vp_fetch"] = time.time()
                # Two vehicles can carry the same trip_id (a trip handed between
                # buses, or overlapping runs); the cache is keyed by trip_id, so
                # the later one wins. That collapse — not a dropped position — is
                # what separates `positioned` from `cached`.
                dup = n_pos - len(cache)
                st["vp_stats"] = {
                    "vehicles": n_total,
                    "positioned": n_pos,
                    "cached": len(cache),
                    "duplicate_trip_id": dup,
                    "without_position": n_no_pos,
                    "without_trip_id": n_no_trip,
                    **({"errors": errors} if errors else {}),
                }
                if errors:
                    print(f"[poll:{rid}] vehicle-position feed(s) failed: "
                          + " | ".join(errors))
                print(f"[vp:{rid}] {n_total} vehicles: {n_pos} positioned, {len(cache)} cached"
                      + (f", {dup} dup trip_id" if dup else "")
                      + (f", {n_no_pos} WITHOUT position" if n_no_pos else "")
                      + (f", {n_no_trip} without trip_id" if n_no_trip else ""))
                # A live vehicle with no coordinates cannot go on the map. It
                # has never happened on these feeds; if it starts, the alarm:
                if n_no_pos:
                    print(f"[vp:{rid}] WARNING: {n_no_pos} live vehicles broadcast "
                          f"with no position and were dropped from the map")
            except Exception as exc:  # the board must survive a map outage
                print(f"[poll:{rid}] vehicle positions fetch failed: {exc}")
            await asyncio.sleep(POLL_SECONDS)


# GTFS-RT Alert.Effect enum -> a short human label for the popup.
ALERT_EFFECT = {
    1: "No service", 2: "Reduced service", 3: "Significant delays",
    4: "Detour", 5: "Additional service", 6: "Modified service",
    7: "Service change", 8: "Service change", 9: "Stop moved",
}


def _alert_text(translated) -> str:
    return translated.translation[0].text if translated.translation else ""


async def poll_alerts(rid: str) -> None:
    """Service disruptions. Only alerts active *now* are kept — the feed also
    carries future planned works, which would swamp the board with warnings
    about next month."""
    cfg, st = REGIONS[rid], STATE[rid]
    # Third slot in the boot stagger (see poll_vehicle_positions).
    await asyncio.sleep(2 * POLL_SECONDS / 3)
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                now = time.time()
                alerts: list = []
                by_route: dict = {}
                by_stop: dict = {}
                n_total = n_inactive = 0
                feeds, errors = await _fetch_feeds(client, cfg, "alerts")
                for prefix, feed in feeds:
                    for entity in feed.entity:
                        if not entity.HasField("alert"):
                            continue
                        a = entity.alert
                        n_total += 1
                        # No active_period at all means "always active".
                        if a.active_period and not any(
                            (p.start or 0) <= now and (not p.end or now <= p.end)
                            for p in a.active_period
                        ):
                            n_inactive += 1
                            continue
                        idx = len(alerts)
                        alerts.append(
                            {
                                "header": _alert_text(a.header_text),
                                "description": _alert_text(a.description_text),
                                "effect": ALERT_EFFECT.get(a.effect, "Service change"),
                            }
                        )
                        for ie in a.informed_entity:
                            if ie.route_id:
                                by_route.setdefault(prefix + ie.route_id, []).append(idx)
                            if ie.stop_id:
                                by_stop.setdefault(prefix + ie.stop_id, []).append(idx)
                if feeds:
                    st["al"] = {"alerts": alerts, "by_route": by_route, "by_stop": by_stop}
                    st["al_fetch"] = time.time()
                st["al_stats"] = {"alerts": n_total, "active": len(alerts),
                                  "not_yet_active": n_inactive,
                                  **({"errors": errors} if errors else {})}
                print(f"[al:{rid}] {n_total} alerts: {len(alerts)} active"
                      + (f", {n_inactive} outside their active period" if n_inactive else ""))
                if errors:
                    print(f"[poll:{rid}] alert feed(s) failed: "
                          + " | ".join(errors))
            except Exception as exc:  # alerts are an enhancement, never fatal
                print(f"[poll:{rid}] alerts fetch failed: {exc}")
            await asyncio.sleep(ALERT_POLL_SECONDS)
