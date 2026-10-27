import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta

import hhan_pzone_core as core
import hhan_pzone_manager as manager

GIB = 1024 ** 3

class Response:
    def __init__(self, data=None, text="Ok.", status=200):
        self.data, self.text, self.status_code = data, text, status
    def json(self):
        if self.data is None:
            raise ValueError()
        return self.data
    def raise_for_status(self):
        if self.status_code >= 400:
            raise core.requests.HTTPError()

class Session:
    def __init__(self, torrents=None, comment="", add_text="Ok.", hash_exists=False):
        self.torrents = torrents or []
        self.comment, self.add_text, self.hash_exists = comment, add_text, hash_exists
        self.posts, self.properties_calls = [], 0
    def get(self, url, params=None, **kwargs):
        if url.endswith("/properties"):
            self.properties_calls += 1
            return Response({"comment": self.comment})
        if params and "hashes" in params:
            return Response([{"hash": params["hashes"]}] if self.hash_exists else [])
        return Response(self.torrents)
    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return Response(text=self.add_text if url.endswith("/add") else "")

def torrent(h="a"*40, size=100, progress=1.0, name="Renamed", category=None):
    return {"hash": h, "name": name, "size": int(size*GIB), "total_size": int(size*GIB),
            "category": category or core.CONFIG["category_keep"], "tracker": "https://tracker.hhanclub.net/announce",
            "progress": progress, "state": "uploading" if progress == 1 else "downloading", "num_complete": 4}

def item(h, size, density=1, curr=4, protected=False):
    return {"key": "existing:"+h, "source": "existing", "hash": h, "name": h,
            "title": h, "size_gb": size, "portfolio_value": size*density,
            "value_per_gb": density, "init_seeders": 4, "curr_seeders": curr,
            "protected": protected, "protect_reason": ""}

def candidate(tid="42", size=60, seeders=2):
    return {"id": tid, "title": "Candidate", "size_gb": size, "seeders": seeders,
            "age_weeks": None, "published_time": None, "value_per_gb": 1, "portfolio_value": size}

def bencode(value):
    if isinstance(value, int): return b"i"+str(value).encode()+b"e"
    if isinstance(value, bytes): return str(len(value)).encode()+b":"+value
    if isinstance(value, list): return b"l"+b"".join(bencode(v) for v in value)+b"e"
    return b"d"+b"".join(bencode(k)+bencode(v) for k,v in sorted(value.items()))+b"e"

def torrent_bytes(size=60):
    return bencode({b"announce": b"https://tracker.hhanclub.net/announce",
                    b"info": {b"length": int(size*GIB), b"name": b"test", b"piece length": 16384, b"pieces": b"x"*20}})

def page(tid=None, maxpage=None, extra=""):
    row = "" if tid is None else f'<tr><td>{tid}</td><td><a href="details.php?id={tid}">Title {tid}</a></td><td>10 GB</td><td>2</td><td>4</td><td>2020-01-01 00:00</td><td>12.5</td><td>19:00</td><td>是</td></tr>'
    return ('<html><script>var maxpage='+str(maxpage)+';</script>' if maxpage is not None else '<html>') + '<table><tr><th>ID</th><th>名称</th><th>大小</th><th>初始人数</th><th>当前人数</th><th>完成时间</th><th>上次结算</th><th>今日做种</th><th>今日达标</th></tr>'+row+'</table>'+extra+'</html>'

class V6Tests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(core.CONFIG)
        self.old_cache, self.old_readonly = core.CACHE_PATH, core.CACHE_READ_ONLY
        self.temp = tempfile.TemporaryDirectory()
        core.CACHE_PATH = os.path.join(self.temp.name, "cache.json")
        core.CACHE_READ_ONLY = False
        core.LAST_PORTFOLIO_CONTEXT = {}
        core.LAST_STRATEGY_CONTEXT = {}
        core.CONFIG.update({"max_preservation_space_gb": 100, "max_downloading_space_gb": 50,
            "protect_init_one": False, "protect_unknown_records": False,
            "portfolio_unit_gb": 1, "space_min_saving_gb": 5, "user_id": "1"})
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.snapshot = patch.object(manager, "fetch_site_points_snapshot", return_value={"rescue_near_cap": False, "rescue_day": None})
        self.snapshot.start()
    def tearDown(self):
        self.snapshot.stop()
        self.output.__exit__(None,None,None)
        core.CONFIG.clear(); core.CONFIG.update(self.config)
        core.CACHE_PATH, core.CACHE_READ_ONLY = self.old_cache, self.old_readonly
        self.temp.cleanup()
    def test_metric_current_factor_initial_multiplier(self):
        a = core.calc_preservation_metrics(10,None,1,2)
        b = core.calc_preservation_metrics(10,None,20,2)
        self.assertGreater(a["portfolio_value"], b["portfolio_value"])
        self.assertEqual(a["multiplier"], b["multiplier"])
        self.assertEqual(a["time_factor"], 1)
    def test_completed_time_never_used_as_publication(self):
        r = {"size_gb": 10, "init_seeders": 2, "curr_seeders": 4, "completed_time": "2026-10-26 00:00", "age_weeks": 0.1}
        core.normalize_record_metrics(r)
        self.assertIsNone(r["age_weeks"])
        self.assertAlmostEqual(r["value_per_gb"], core.calc_preservation_metrics(10,None,4,2)["value_per_gb"], places=5)
    def test_pagination_past_25_and_fields(self):
        calls=[]
        def read(url):
            p = int(url.rsplit("page=",1)[1]); calls.append(p)
            return page(p+1,30)
        with patch.object(core,"curl_get_hhan",side_effect=read):
            result=core.fetch_remote_preservation_records()
        self.assertEqual(len(result),31)
        self.assertEqual(max(calls),30)
        self.assertEqual(result[0]["today_qualified"],"是")
        self.assertEqual(result[0]["last_settlement_points"],12.5)
    def test_pagination_unknown_runs_until_empty(self):
        with patch.object(core,"curl_get_hhan",side_effect=[page(1),page(2),page()]):
            self.assertEqual(len(core.fetch_remote_preservation_records()),2)
    def test_pagination_failure_does_not_publish_partial(self):
        with patch.object(core,"curl_get_hhan",side_effect=[page(1,3), ""]):
            with self.assertRaises(RuntimeError): core.fetch_remote_preservation_records()
    def test_identity_comment_lookup_once_and_duplicate_merge(self):
        payload=core.empty_cache()
        payload["records"]=[{"torrent_id":"42","title":"Different original title","size_gb":100,"init_seeders":2,"source":"remote_action7"},
                             {"hash":"a"*40,"title":"Renamed","size_gb":100,"init_seeders":0}]
        session=Session(comment="https://hhanclub.net/details.php?id=42&hit=1")
        for _ in range(2): core.reconcile_cache_with_qb(payload,[torrent()],session)
        self.assertEqual(len(payload["records"]),1)
        self.assertEqual(payload["records"][0]["init_seeders"],2)
        self.assertEqual(session.properties_calls,1)
    def test_conflicting_ids_not_merged_by_title(self):
        records=[{"torrent_id":"1","title":"same","size_gb":10}]
        self.assertIsNone(core.find_cache_record(records,torrent_id="2",title="same",size_gb=10))
    def test_unknown_does_not_overwrite_real_init(self):
        records=[{"torrent_id":"1","init_seeders":2,"init_seeders_source":"remote_action7"}]
        core.merge_record(records,{"torrent_id":"1","init_seeders":0},"qb")
        core.merge_record(records,{"torrent_id":"1","init_seeders":4},"download_candidate")
        self.assertEqual(records[0]["init_seeders"],2)
    def test_download_excluded_but_full_size_reserved(self):
        payload=core.empty_cache()
        _,size,items,_=core.check_qb_status([torrent(size=90),torrent("b"*40,40,0.5)],payload)
        self.assertEqual(size,90); self.assertEqual(len(items),1)
        self.assertEqual(core.LAST_PORTFOLIO_CONTEXT["downloading_gb"],40)
    def test_overquota_release_only(self):
        core.LAST_PORTFOLIO_CONTEXT={"downloading_gb":0}
        result=manager.generate_strategy([candidate(size=5)],120,[item("a",60),item("b",60)])
        self.assertFalse(result[0]); self.assertTrue(result[2])
    def test_pending_download_prevents_new_space_batch(self):
        core.LAST_PORTFOLIO_CONTEXT={"downloading_count":1,"downloading_gb":10}
        result=manager.generate_strategy([candidate(size=5)],100,[item("a",50),item("b",50)])
        self.assertFalse(result[0])
        self.assertIn("等待完成",result[5]["suppression_reason"])
    def test_space_loss_anchor_does_not_compound(self):
        core.LAST_PORTFOLIO_CONTEXT={"strategy_state":{"reference_value":100,"space_optimized":True}}
        items=[item(str(i),10) for i in range(10)]
        first=manager.generate_strategy([],100,items)
        self.assertEqual(first[3],10)
        removed={i["hash"] for i in first[2]}
        remaining=[i for i in items if i["hash"] not in removed]
        core.LAST_PORTFOLIO_CONTEXT={"strategy_state":core.LAST_STRATEGY_CONTEXT["strategy_state"]}
        second=manager.generate_strategy([],90,remaining)
        self.assertFalse(second[2])
        self.assertEqual(second[5]["value_floor"],90)
    def test_capped_portfolio_can_compress(self):
        manager.fetch_site_points_snapshot.return_value={"rescue_near_cap":True,"rescue_day":1800}
        result=manager.generate_strategy([],100,[item(str(i),10) for i in range(10)])
        self.assertEqual(result[3],10)
        self.assertIn("CAP-GUARD",result[5]["suppression_reason"])
    def test_min_space_dp_keeps_value(self):
        selected=core.knapsack_select([item("a",10,1),item("b",6,2)],20,1,target_value=10)
        self.assertEqual([x["hash"] for x in selected],["b"])
    def test_torrent_hash_and_accurate_size(self):
        data=torrent_bytes(37)
        metadata=core.torrent_metadata(data)
        self.assertEqual(metadata["size_gb"],37)
        info=data[data.index(b"4:info")+6:-1]
        self.assertEqual(metadata["hash"],hashlib.sha1(info).hexdigest())
    def test_torrent_v2(self):
        info={b"meta version":2,b"name":b"v2",b"file tree":{b"file":{b"":{b"length":GIB}}}}
        metadata=core.torrent_metadata(bencode({b"info":info}))
        self.assertEqual(metadata["size_gb"],1)
        self.assertEqual(len(metadata["hash_v2"]),64)
        self.assertEqual(metadata["hash"],metadata["hash_v2"][:40])
    def test_invalid_torrent_rejected(self):
        with self.assertRaises(ValueError): core.torrent_metadata(b"<html>login</html>")
    def test_fails_not_assumed_existing(self):
        session=Session(add_text="Fails.")
        with patch.object(core,"curl_download_torrent",return_value=torrent_bytes(10)):
            core.execute_actions(session,[candidate(size=10)],[])
        self.assertEqual(core.load_cache()["records"][0]["add_status"],"failed")
        self.assertFalse(any(url.endswith("setCategory") for url,_ in session.posts))
    def test_fails_hash_confirmed_is_existing(self):
        session=Session(add_text="Fails.",hash_exists=True)
        with patch.object(core,"curl_download_torrent",return_value=torrent_bytes(10)):
            core.execute_actions(session,[candidate(size=10)],[])
        self.assertEqual(core.load_cache()["records"][0]["add_status"],"existing")
    def test_add_uses_known_bytes_without_immediate_qb_reread(self):
        session=Session()
        with patch.object(core,"curl_download_torrent",return_value=torrent_bytes(20)) as dl:
            core.execute_actions(session,[candidate(size=10)],[])
        record=core.load_cache()["records"][0]
        self.assertEqual(record["size_gb"],20)
        self.assertEqual(record["add_status"],"submitted")
        self.assertEqual(dl.call_count,1)
        self.assertTrue(record["hash"])
    def test_actual_size_cannot_overrun_buffer(self):
        session=Session()
        with patch.object(core,"curl_download_torrent",return_value=torrent_bytes(60)):
            core.execute_actions(session,[candidate(size=10)],[])
        self.assertFalse(session.posts)
    def test_dry_run_does_not_write_cache(self):
        core.CACHE_READ_ONLY=True
        core.save_cache({"records":[]})
        self.assertFalse(os.path.exists(core.CACHE_PATH))
    def test_config_overrides_validate(self):
        import argparse
        parser=argparse.ArgumentParser()
        core.apply_config_overrides(["space_reward_loss_ratio=0.1","max_downloading_space_gb=300"],parser)
        self.assertEqual(core.CONFIG["max_downloading_space_gb"],300)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit): core.apply_config_overrides(["space_reward_loss_ratio=nan"],parser)

    def test_full_dry_run_leaves_existing_cache_identical(self):
        import sys
        cache=core.empty_cache(); cache["records"]=[{"hash":"a"*40,"torrent_id":"1","init_seeders":4,"size_gb":90,"title":"Renamed"}]
        core.save_cache(cache)
        before=Path(core.CACHE_PATH).read_bytes()
        with patch.object(core,"get_qb_session",return_value=Session([torrent(size=90)])), patch.object(core,"fetch_remote_preservation_records",return_value=[]), patch.object(core,"fetch_rescue_candidates",return_value=([],{"eligible_count":0,"already_have":0})), patch.object(sys,"argv",["manager","--set","space_reward_loss_ratio=0.05"]):
            core.main()
        self.assertEqual(Path(core.CACHE_PATH).read_bytes(),before)
    def test_downloading_task_never_in_deletion_plan(self):
        torrents=[torrent("a"*40,100),torrent("b"*40,50,0.2)]
        _,size,items,_=core.check_qb_status(torrents,core.empty_cache())
        result=manager.generate_strategy([],size,items)
        self.assertTrue(all(x["hash"]!="b"*40 for x in result[2]))
    def test_buffer_applies_to_total_not_each_batch(self):
        core.LAST_PORTFOLIO_CONTEXT={"downloading_gb":45,"downloading_count":1}
        result=manager.generate_strategy([candidate(size=10)],20,[item("a",20)])
        self.assertFalse(result[0])
    def test_repeated_candidate_id_deduplicated(self):
        card='<div class="torrent-table-sub-info"><a class="torrent-info-text-name" href="details.php?id=42">A</a><div class="torrent-info-text-size">10 GB</div><div class="torrent-info-text-seeders">2</div></div>'
        with patch.object(core,"curl_get_hhan",return_value=card+card):
            candidates,stats=core.fetch_rescue_candidates(set())
        self.assertEqual(len(candidates),1)
        with patch.object(core,"curl_get_hhan",return_value=card):
            candidates,stats=core.fetch_rescue_candidates(set(),existing_ids={"42"})
        self.assertFalse(candidates)
    def test_inflight_submission_excluded_by_id(self):
        core.LAST_PORTFOLIO_CONTEXT={"records":[{"torrent_id":"42","add_status":"submitted"}]}
        card='<div class="torrent-table-sub-info"><a class="torrent-info-text-name" href="details.php?id=42">A</a></div>'
        with patch.object(core,"curl_get_hhan",return_value=card):
            self.assertFalse(core.fetch_rescue_candidates(set())[0])
    def test_same_tier_small_candidate_allowed_without_gain_threshold(self):
        core.LAST_PORTFOLIO_CONTEXT={"strategy_state":{"space_optimized":True,"reference_value":100}}
        c=candidate(size=40,seeders=4)
        result=manager.generate_strategy([c],100,[item("a",100)])
        self.assertTrue(result[0])
        self.assertFalse(result[2])  # Current floor cannot be backed by unfinished new torrent.
    def test_precise_tie_retains_lower_current_seeders(self):
        keep=manager._minimum_portfolio([], [item("high",10,1,curr=20),item("low",10,1,curr=2)],20,10)
        self.assertEqual(keep[0]["hash"],"low")
    def test_stale_execution_plan_aborts(self):
        core.LAST_STRATEGY_CONTEXT={"completed_hashes":["a"*40]}
        session=Session([torrent("b"*40)])
        core.execute_actions(session,[],[{"hash":"a"*40}])
        self.assertFalse(session.posts)
    def test_pending_not_visible_size_counted_once(self):
        data=torrent_bytes(20); meta=core.torrent_metadata(data)
        core.upsert_candidate_cache(dict(candidate(size=20),**meta,add_status="submitted"))
        payload=core.load_cache()
        _,_,_,_=core.check_qb_status([],payload)
        self.assertEqual(core.LAST_PORTFOLIO_CONTEXT["downloading_gb"],20)
    def test_foreign_comment_link_not_used(self):
        self.assertIsNone(core.torrent_id_from_comment("https://example.com/details.php?id=42"))

    def test_completed_new_and_old_overlap_does_not_raise_reference(self):
        core.LAST_PORTFOLIO_CONTEXT={"strategy_state":{"space_optimized":True,"reference_value":100}}
        result=manager.generate_strategy([],120,[item("old",80),item("new",40)])
        self.assertEqual(result[5]["reference_value"],100)
        self.assertEqual(result[5]["value_floor"],90)
    def test_execute_space_release_updates_ledger_without_post_add_read(self):
        records=[{"hash":h*40,"torrent_id":h,"size_gb":10,"init_seeders":4,"curr_seeders":4,"active":True} for h in ("a","b")]
        core.save_cache({"records":records})
        actual=core.calc_preservation_metrics(10,None,4,4)["portfolio_value"]
        core.LAST_STRATEGY_CONTEXT={"completed_hashes":["a"*40,"b"*40],"value_floor":actual}
        session=Session([torrent("a"*40,10),torrent("b"*40,10)])
        core.execute_actions(session,[],[dict(item("a"*40,10),reason="SPACE")])
        self.assertTrue(session.posts[0][0].endswith("setCategory"))
        self.assertFalse(core.load_cache()["records"][0]["active"])
    def test_execute_rechecks_actual_value_instead_of_stale_plan_value(self):
        records=[{"hash":h*40,"torrent_id":h,"size_gb":10,"init_seeders":4,"curr_seeders":4} for h in ("a","b")]
        core.save_cache({"records":records})
        actual=core.calc_preservation_metrics(10,None,4,4)["portfolio_value"]
        core.LAST_STRATEGY_CONTEXT={"completed_hashes":["a"*40,"b"*40],"value_floor":actual*1.5}
        session=Session([torrent("a"*40,10),torrent("b"*40,10)])
        stale=dict(item("a"*40,10),reason="SPACE",portfolio_value=0)
        core.execute_actions(session,[],[stale])
        self.assertFalse(session.posts)

    def test_actual_site_headers_page_count_and_settlement_time(self):
        fixture='<table><tbody><tr><th>种子ID</th><th>种子名称</th><th>种子大小</th><th>初始保种人数</th><th>现在保种人数</th><th>完成时间</th><th>上次结算时间</th><th>今日做种时间</th><th>今日是否达标</th></tr><tr class="text-center"><td>5732</td><td><a href="details.php?id=5732">Fixture title</a></td><td>6.87 GB</td><td>4</td><td>10</td><td>2025-03-15 14:30</td><td>2026-10-27 00:00</td><td>0:00</td><td><span class="font-bold">未达标</span></td></tr></tbody></table><input type="number" min="1" max="1" value="1">'
        with patch.object(core,"curl_get_hhan",return_value=fixture) as read:
            records=core.fetch_remote_preservation_records()
        self.assertEqual(read.call_count,1)
        self.assertEqual(records[0]["last_settlement_time"],"2026-10-27 00:00")
        self.assertEqual(records[0]["today_qualified"],"未达标")

if __name__=="__main__": unittest.main()
