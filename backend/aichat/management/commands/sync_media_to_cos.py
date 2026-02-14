import os
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand
from common.cos_utils import upload_file_to_cos


class Command(BaseCommand):
    help = "Upload local MEDIA_ROOT files to COS (keeps relative paths)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List files that would be uploaded without performing uploads.",
        )
        parser.add_argument(
            "--prefix",
            default=None,
            help="Optional override for COS_UPLOAD_PREFIX (keeps MEDIA_ROOT relative path).",
        )
        parser.add_argument(
            "--exclude-prefix",
            action="append",
            default=[],
            help="Exclude relative path prefixes under MEDIA_ROOT. Can be repeated.",
        )

    def handle(self, *args, **options):
        media_root = getattr(settings, "MEDIA_ROOT", None)
        if not media_root:
            self.stderr.write("MEDIA_ROOT is not configured.")
            return
        media_root = Path(media_root)
        if not media_root.exists():
            self.stderr.write(f"MEDIA_ROOT does not exist: {media_root}")
            return

        dry_run = options["dry_run"]
        prefix_override = options.get("prefix")
        excludes = [p.strip("/").strip() for p in options.get("exclude_prefix", []) if p.strip()]
        total = uploaded = failed = 0

        for root, _, files in os.walk(media_root):
            for fname in files:
                if fname.startswith("."):
                    continue
                local_path = Path(root) / fname
                rel = local_path.relative_to(media_root).as_posix()
                if any(rel.startswith(e) for e in excludes):
                    continue
                key = f"{prefix_override.strip('/')}/{rel}" if prefix_override else rel
                total += 1
                if dry_run:
                    self.stdout.write(f"[DRY] {local_path} -> {key}")
                    continue
                url = upload_file_to_cos(str(local_path), key)
                if url:
                    uploaded += 1
                    self.stdout.write(f"[OK] {rel} -> {url}")
                else:
                    failed += 1
                    self.stderr.write(f"[FAIL] {rel}")

        if dry_run:
            self.stdout.write(f"Dry run complete. Files found: {total}")
        else:
            self.stdout.write(
                f"Done. total={total}, uploaded={uploaded}, failed={failed}"
            )
