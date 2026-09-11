#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dirty-write policy for user_preservation_cache.json.

The qB reconciliation still runs on every manager invocation.  This module only
suppresses disk writes when the persistent ledger has no meaningful change.
Volatile observation fields are intentionally ignored so the cache mtime tracks
real ledger changes instead of script launches.
"""

import json
import os


TOP_LEVEL_VOLATILE_FIELDS = {
    "updated_at",
    "qb_reconciled_at",
}

RECORD_VOLATILE_FIELDS = {
    "last_seen",
    "last_active",
    "state",
    "progress",
}


def _semantic_record(record):
    if not isinstance(record, dict):
        return record
    return {
        key: value
        for key, value in record.items()
        if key not in RECORD_VOLATILE_FIELDS
    }


def semantic_cache_view(payload):
    """Return only fields whose change should cause a persistent cache write."""
    if not isinstance(payload, dict):
        return payload

    view = {}
    for key, value in payload.items():
        if key in TOP_LEVEL_VOLATILE_FIELDS:
            continue
        if key == "records" and isinstance(value, list):
            view[key] = [_semantic_record(record) for record in value]
        else:
            view[key] = value
    return view


def install(core):
    """Patch core.save_cache so unchanged reconciliations do not touch the file."""
    original_save_cache = core.save_cache

    def save_cache_if_dirty(payload):
        disk_view = None
        if os.path.exists(core.CACHE_PATH):
            try:
                with open(core.CACHE_PATH, "r", encoding="utf-8") as handle:
                    disk_view = semantic_cache_view(json.load(handle))
            except Exception:
                # Invalid/unreadable cache should be repaired by a normal write.
                disk_view = None

        memory_view = semantic_cache_view(payload)
        if disk_view is not None and disk_view == memory_view:
            core.LAST_CACHE_WRITE = False
            return False

        original_save_cache(payload)
        core.LAST_CACHE_WRITE = True
        return True

    core.save_cache = save_cache_if_dirty
    core.CACHE_WRITE_POLICY = "dirty-only"
    core.LAST_CACHE_WRITE = None
    return core
