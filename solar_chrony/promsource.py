"""Fetch raw samples from Prometheus."""

import json
import urllib.parse
import urllib.request


class Prometheus:
    def __init__(self, url: str, timeout: float = 120.0):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def raw(self, selector: str, start: float, end: float, key_label: str | None = None
            ) -> dict[str, list[tuple[float, float]]]:
        """Raw (unaggregated) samples in [start, end), keyed by `key_label`."""
        seconds = max(1, int(round(end - start)))
        query = urllib.parse.urlencode({"query": f"{selector}[{seconds}s]", "time": f"{end - 0.001:.3f}"})
        with urllib.request.urlopen(f"{self.url}/api/v1/query?{query}", timeout=self.timeout) as resp:
            data = json.load(resp)
        if data.get("status") != "success":
            raise RuntimeError(f"prometheus query failed: {data.get('error', data)}")
        out: dict[str, list[tuple[float, float]]] = {}
        for series in data["data"]["result"]:
            key = series["metric"].get(key_label, "") if key_label else ""
            rows = out.setdefault(key, [])
            rows.extend((float(t), float(v)) for t, v in series["values"] if float(t) >= start)
        for rows in out.values():
            rows.sort()
        return out
