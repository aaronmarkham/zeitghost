"""One-time migration: backfill tags onto existing shards via lineage.

Shards written before the lineage+tags commit lack both. This script does NOT
modify shards in place (they're immutable) — for each existing shard without
tags, it writes a NEW child shard with the same atoms + computed tags +
`parent_shard_id` back to the original. Old shards remain queryable in their
original form; new shards become the "latest revision" for each entity and
participate in normal lineage chains going forward.

Idempotent: shards that already have tags are skipped, so re-running the
migration after a partial run (or any time later) won't keep stacking
revisions on top of each other.

Workflow on us-ny1:

    docker exec zeitghost-builder python -m scripts.augment_shard_tags --dry-run
    # If the counts look right:
    docker exec zeitghost-builder python -m scripts.augment_shard_tags

After completion, `zeitghost build` will re-render the site from the
new latest-revision shards (functionally identical, but now tagged).
"""

from __future__ import annotations

import logging

from spiritwriter.trace.shard import MemoryShard

from zeitghost.shards import (
    init_store, tags_from_shard,
    SCOPE_INTERNAL, SCOPE_SW_ARTICLE,
)

log = logging.getLogger(__name__)


def augment_scope(scope: str, *, dry_run: bool = False, console=None) -> dict:
    """Walk every shard in `scope`. For each one without tags, write a child
    shard with computed tags + parent_shard_id back to the predecessor.

    Returns a stats dict.
    """
    store = init_store()

    # SNAPSHOT the list of shards to migrate before writing anything. If we
    # iterated `store.by_scope(scope)` lazily and put new shards mid-loop, the
    # iterator could observe its own writes and we'd risk infinite revisions.
    to_migrate: list[MemoryShard] = [
        s for s in store.by_scope(scope) if not s.tags
    ]
    skipped_already_tagged = sum(1 for _ in store.by_scope(scope)) - len(to_migrate)

    if console:
        console.print(
            f"[bold]Scope {scope}:[/bold] {len(to_migrate)} shards to augment "
            f"({skipped_already_tagged} already have tags — skipped)"
        )

    augmented = 0
    skipped_no_tags = 0

    for shard in to_migrate:
        new_tags = tags_from_shard(shard)
        if not new_tags:
            # Couldn't extract source_name/categories/published from atoms —
            # rare, but possible for malformed legacy shards. Don't write a
            # tagless revision (it'd just be a noop).
            skipped_no_tags += 1
            continue

        if dry_run:
            augmented += 1
            continue

        # New shard with same atoms + tags + parent_shard_id back to the
        # original. spiritwriter computes a new shard_id from the changed
        # contents (tags + parent differ even if atoms match).
        new_shard = MemoryShard(
            atoms=list(shard.atoms),
            scope=shard.scope,
            origin=shard.origin or "zeitghost",
            decay_class=shard.decay_class,
            parent_shard_id=shard.shard_id,
            tags=new_tags,
            meta=dict(shard.meta or {}),
        )
        store.put(new_shard)
        augmented += 1

        if console and augmented % 500 == 0:
            console.print(f"  augmented {augmented} so far...")

    return {
        "scope": scope,
        "to_migrate": len(to_migrate),
        "augmented": augmented,
        "skipped_already_tagged": skipped_already_tagged,
        "skipped_no_tags_extractable": skipped_no_tags,
    }


def run(dry_run: bool = False, console=None) -> list[dict]:
    """Augment both scopes in the conventional order. Returns the per-scope
    stats dicts."""
    return [
        augment_scope(SCOPE_INTERNAL, dry_run=dry_run, console=console),
        augment_scope(SCOPE_SW_ARTICLE, dry_run=dry_run, console=console),
    ]


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be augmented without writing shards")
    args = p.parse_args()
    results = run(dry_run=args.dry_run)
    verb = "Would augment" if args.dry_run else "Augmented"
    total = sum(r["augmented"] for r in results)
    skipped = sum(r["skipped_already_tagged"] for r in results)
    print(f"\n{verb} {total} shards across {len(results)} scopes "
          f"({skipped} already tagged, skipped)")
    for r in results:
        print(f"  {r['scope']:25} {r['augmented']:>6} augmented, "
              f"{r['skipped_already_tagged']:>6} already tagged, "
              f"{r['skipped_no_tags_extractable']:>3} no tags extractable")
