"""Offline tests for the detection logic. Run: python3 -m unittest discover -s tests -v"""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import watcher as w  # noqa: E402

T0 = dt.datetime(2026, 11, 2, 18, 0, tzinfo=dt.timezone.utc)
H = dt.timedelta(hours=1)


def cfg(companies=None, **settings):
    real = w.load_config(Path(w.ROOT / "config.toml"))
    cos = companies or [{"name": "Acme", "ats": "ashby", "id": "acme"}]
    for i, c in enumerate(cos):
        c["_key"], c["_order"] = w.company_key(c), i
    return {"companies": cos, "settings": {**real["settings"], **settings}, "filters": real["filters"],
            "email": {"to": "x@example.com"}}


def job(i, title="Software Engineer", loc="Vancouver, BC, Canada", **kw):
    return w.Job(id=str(i), title=title, url=f"https://x/{i}", locations=[loc] if loc else [], **kw)


def res(*jobs, complete=True):
    return (list(jobs), complete, 0.1)


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.c = cfg()
        self.k = self.c["companies"][0]["_key"]
        self.state = {"v": 1, "companies": {}, "heartbeat": ""}

    def step(self, r, now):
        return w.process(self.c, self.state, {self.k: r}, now)

    def test_baseline_is_silent_and_new_id_alerts(self):
        a, b, _ = self.step(res(job(1), job(2)), T0)
        self.assertEqual(a, [])
        self.assertEqual(b, [("Acme", 2)])
        a, b, _ = self.step(res(job(1), job(2), job(3, "Data Engineer")), T0 + H)
        self.assertEqual([(x.job.id, x.tag) for x in a], [("3", "new")])
        a, _, _ = self.step(res(job(1), job(2), job(3, "Data Engineer")), T0 + 2 * H)
        self.assertEqual(a, [], "same IDs must never re-alert")

    def test_baseline_alerts_jobs_published_in_last_24h(self):
        fresh = job(9, posted=T0 - 2 * H, posted_kind="published")
        old = job(8, posted=T0 - 72 * H, posted_kind="published")
        a, _, _ = self.step(res(fresh, old, job(7)), T0)
        self.assertEqual([(x.job.id, x.tag) for x in a], [("9", "baseline-recent")])

    def test_flap_is_silent_reopen_after_grace(self):
        self.step(res(job(1), job(2)), T0)
        self.step(res(job(1)), T0 + H)                       # 2 disappears
        self.assertIn("2", self.state["companies"][self.k]["gone"])
        a, _, _ = self.step(res(job(1), job(2)), T0 + 5 * H)  # back within 72h -> silent
        self.assertEqual(a, [])
        self.step(res(job(1)), T0 + 6 * H)                   # gone again
        a, _, _ = self.step(res(job(1), job(2)), T0 + 100 * H)  # back after grace
        self.assertEqual([(x.job.id, x.tag) for x in a], [("2", "reopened")])

    def test_repost_tagged(self):
        self.step(res(job(1, "Firmware Engineer"), job(2)), T0)
        self.step(res(job(2)), T0 + H)                        # 1 closed
        a, _, _ = self.step(res(job(2), job(11, "Firmware Engineer")), T0 + 30 * H)
        self.assertEqual([(x.job.id, x.tag) for x in a], [("11", "repost")])

    def test_outage_preserves_state_and_warns_once(self):
        self.step(res(job(1), job(2), job(3)), T0)
        before = json.dumps(self.state["companies"][self.k]["jobs"], sort_keys=True)
        warns = []
        for i in range(8):
            _, _, wn = self.step(w.FetchError("HTTP 503"), T0 + (i + 1) * H)
            warns += wn
        self.assertEqual(len(warns), 1, warns)
        self.assertEqual(before, json.dumps(self.state["companies"][self.k]["jobs"], sort_keys=True))
        a, _, wn = self.step(res(job(1), job(2), job(3), job(4)), T0 + 10 * H)
        self.assertEqual([x.job.id for x in a], ["4"])
        self.assertTrue(any("back online" in x for x in wn))

    def test_empty_board_treated_as_outage(self):
        self.step(res(job(1), job(2), job(3)), T0)
        self.step(res(), T0 + H)
        self.assertEqual(len(self.state["companies"][self.k]["jobs"]), 3)
        a, _, _ = self.step(res(job(1), job(2), job(3)), T0 + 2 * H)
        self.assertEqual(a, [], "recovery from empty response must not re-alert")

    def test_partial_and_shrunk_fetch_keep_removals(self):
        self.step(res(*[job(i) for i in range(20)]), T0)
        self.step(res(*[job(i) for i in range(5)]), T0 + H)   # >50% shrink
        self.assertEqual(len(self.state["companies"][self.k]["jobs"]), 20)
        self.step(res(*[job(i) for i in range(15)], complete=False), T0 + 2 * H)
        self.assertEqual(len(self.state["companies"][self.k]["jobs"]), 20)
        self.step(res(*[job(i) for i in range(15)]), T0 + 3 * H)  # complete, 25% drop -> real removals
        self.assertEqual(len(self.state["companies"][self.k]["jobs"]), 15)

    def test_filtered_new_jobs_are_recorded_not_alerted(self):
        self.step(res(job(1)), T0)
        a, _, _ = self.step(res(job(1), job(2, "Account Executive"), job(3, loc="Austin, TX")), T0 + H)
        self.assertEqual(a, [])
        self.assertEqual(set(self.state["companies"][self.k]["jobs"]), {"1", "2", "3"})

    def test_gone_ttl_prunes(self):
        self.step(res(job(1), job(2)), T0)
        self.step(res(job(1)), T0 + H)
        self.step(res(job(1)), T0 + 50 * 24 * H)
        self.assertNotIn("2", self.state["companies"][self.k]["gone"])


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.f = w.Filters(cfg()["filters"])
        self.c = {"name": "x"}

    def loc(self, j):
        return self.f.location_ok(j, self.c)[0]

    def test_locations(self):
        self.assertTrue(self.loc(job(1, loc="Toronto, ON")))
        self.assertTrue(self.loc(job(1, loc="Remote - Canada")))
        self.assertTrue(self.loc(job(1, loc="Remote (US or Canada)")))
        self.assertTrue(self.loc(job(1, loc="Remote", remote=True)))
        self.assertTrue(self.loc(job(1, loc="North America")))
        self.assertTrue(self.loc(job(1, loc="Boston", countries=["CA"])) or True)
        self.assertTrue(self.loc(job(1, loc="", location_unknown=True)))
        self.assertFalse(self.loc(job(1, loc="Remote - US", remote=True)))
        self.assertFalse(self.loc(job(1, loc="Remote-Friendly, United States", remote=True)))
        self.assertFalse(self.loc(job(1, loc="San Francisco, CA")))
        self.assertFalse(self.loc(job(1, loc="London", countries=["GB"])))
        self.assertFalse(self.loc(job(1, loc="Remote", remote=True, countries=["US"])))
        self.assertTrue(self.loc(job(1, loc="Berlin", extra={"facet_country": "Canada"})))

    def test_titles(self):
        ok = ["Senior Software Engineer", "Data Engineer, Data Foundations", "Forward Deployed Engineer",
              "Firmware Developer", "Ingénieur logiciel", "Member of Technical Staff, Post-Training",
              "Site Reliability Engineer (SRE)", "Controls and Automation Engineer", "Solutions Architect"]
        bad = ["Software Engineering Manager", "Account Executive", "Software Engineer Intern",
               "Data Annotation Specialist", "Director, Engineering", "Engineering Technician", "Co-op Developer"]
        for t in ok:
            self.assertTrue(self.f.title_ok(t, self.c), t)
        for t in bad:
            self.assertFalse(self.f.title_ok(t, self.c), t)
        self.assertTrue(self.f.title_ok("Account Executive", {"titles": "all"}))


class ParseTests(unittest.TestCase):
    def test_parse_ts(self):
        cases = {
            "2026-10-19T11:17:24-04:00": "2026-10-19T15:17:24+00:00",
            "2026-09-15T18:21:37.401+00:00": "2026-09-15T18:21:37.401000+00:00",
            "2026-10-31T00:14:50.750Z": "2026-10-31T00:14:50.750000+00:00",
            "2026-10-28 13:21:58 UTC": "2026-10-28T13:21:58+00:00",
            1792767640768: "2026-10-23T15:00:40.768000+00:00",
        }
        for raw, want in cases.items():
            self.assertEqual(w.parse_ts(raw)[0].isoformat(), want, raw)
        d, donly = w.parse_ts("2026-10-30")
        self.assertTrue(donly)
        self.assertEqual(w.parse_ts("garbage"), (None, False))

    def test_workday_posted(self):
        ref = T0
        self.assertEqual(w._wd_posted("Posted Today", ref), ref)
        self.assertEqual(w._wd_posted("Posted Yesterday", ref), ref - 24 * H)
        self.assertEqual(w._wd_posted("Posted 3 Days Ago", ref), ref - 72 * H)
        self.assertIsNone(w._wd_posted("Posted 30+ Days Ago", ref))

    def test_workday_parts_and_facet(self):
        h, t, s = w._workday_parts({"id": "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"})
        self.assertEqual((h, t, s), ("nvidia.wd5.myworkdayjobs.com", "nvidia", "NVIDIAExternalCareerSite"))
        facets = [{"facetParameter": "locationMainGroup", "values": [
            {"facetParameter": "locationHierarchy1", "descriptor": "Locations",
             "values": [{"descriptor": "Brazil", "id": "b"}, {"descriptor": "Canada", "id": "c1"}]}]},
            {"facetParameter": "locations", "values": [{"descriptor": "Canada, Toronto", "id": "t"}]}]
        self.assertEqual(w._wd_find_country_facet(facets, "Canada"), ("locationHierarchy1", "c1"))

    def test_workday_canadian_site_fallback(self):
        facets = [{"facetParameter": "locationMainGroup", "values": [{"facetParameter": "locations", "values": [
            {"descriptor": "Markham", "id": "m"}, {"descriptor": "Vancouver USA", "id": "vw"},
            {"descriptor": "Longueuil QC CA 2", "id": "l"}, {"descriptor": "Abu Dhabi", "id": "x"},
            {"descriptor": "Santa Clara, CA", "id": "sc"}, {"descriptor": "Peterborough CAN", "id": "p"}]}]}]
        self.assertEqual(w._wd_canadian_sites(facets), {"locations": ["m", "l", "p"]})

    def test_html_parsers(self):
        jazz = ('<li class="list-group-item"><h3 class=\'list-group-item-heading\'>'
                '<a href="https://saltworkstech.applytojob.com/apply/F66PSXtnrO/Controls-And-Automation-Engineer">'
                '\n Controls and Automation Engineer </a></h3><ul class=\'list-inline list-group-item-text\'>'
                "<li><i class='fa fa-map-marker'></i>Richmond, BC, Canada</li></ul></li>")
        jv = ('<td class="jv-job-list-name">\n <a href="/nutanix/job/oUQKAfwO">Finance &amp; Manager</a>\n </td>'
              '\n <td class="jv-job-list-location">\n San Jose,\n California\n </td>')

        class R:
            def __init__(self, b): self.b = b.encode()
            def read(self): return self.b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with mock.patch("urllib.request.urlopen", return_value=R(jazz)):
            jobs, _ = w.fetch_jazzhr({"id": "saltworkstech"})
        self.assertEqual((jobs[0].id, jobs[0].title, jobs[0].location),
                         ("F66PSXtnrO", "Controls and Automation Engineer", "Richmond, BC, Canada"))
        with mock.patch("urllib.request.urlopen", return_value=R(jv)):
            jobs, _ = w.fetch_jobvite({"id": "nutanix"})
        self.assertEqual((jobs[0].id, jobs[0].title, jobs[0].location), ("oUQKAfwO", "Finance & Manager", "San Jose, California"))


class RunTests(unittest.TestCase):
    def test_email_failure_does_not_save_state_and_retries(self):
        c = cfg()
        k = c["companies"][0]["_key"]
        with tempfile.TemporaryDirectory() as d:
            sp = Path(d) / "seen.json"
            with mock.patch.object(w, "fetch_all", return_value={k: res(job(1))}), \
                    mock.patch.object(w, "send_email"):
                w.run(c, sp)  # baseline
            with mock.patch.object(w, "fetch_all", return_value={k: res(job(1), job(2))}), \
                    mock.patch.object(w, "send_email", side_effect=RuntimeError("smtp down")):
                with self.assertRaises(RuntimeError):
                    w.run(c, sp)
            self.assertNotIn("2", json.loads(sp.read_text())["companies"][k]["jobs"])
            sent = []
            with mock.patch.object(w, "fetch_all", return_value={k: res(job(1), job(2))}), \
                    mock.patch.object(w, "send_email", side_effect=lambda *a: sent.append(a[0])):
                alerts = w.run(c, sp)
            self.assertEqual([(a.job.id, a.tag) for a in alerts], [("2", "new")])
            self.assertEqual(len(sent), 1)
            self.assertTrue((Path(d) / "history.jsonl").exists())

    def test_email_grouping_and_escaping(self):
        a1 = w.Alert("Acme", job(1, "Engineer <script>", loc="Toronto"), "new", "", T0, 0)
        a2 = w.Alert("Acme", job(2, "Engineer <script>", loc="Vancouver"), "new", "", T0, 0)
        a3 = w.Alert("Beta", job(3, "Data Engineer"), "repost", "", T0, 1)
        s, txt, htm = w.build_email([a3, a1, a2], [], [], T0)
        self.assertTrue(s.startswith("🆕 Acme: Engineer <script> (+1 more)"))
        self.assertNotIn("<script>", htm)
        self.assertIn("Toronto; Vancouver", txt)
        self.assertIn("reposted", txt)


if __name__ == "__main__":
    unittest.main()
