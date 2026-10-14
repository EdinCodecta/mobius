"""Write an owner's Store listing edit into an app's editable source.

The listing is ordinary accepted source: text in ``mobius.json``'s ``store``
object, artwork under ``static/store/``. This module only changes the
worktree; the caller accepts the result through the normal apply path, and
uses the returned undo only if acceptance fails before Git advances. A
committed revision remains intact as the normal retry point.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Callable

from app import app_git, icon_assets, models, workspace_files
from app.community_publish import (
  DESCRIPTION_MAX_BYTES,
  MAX_STORE_SCREENSHOTS,
  SCREENSHOT_ALT_MAX_BYTES,
  SCREENSHOT_LABEL_MAX_BYTES,
  TAGLINE_MAX_BYTES,
  CommunityPublicationError,
  check_listing_image,
  listing_image_has_metadata,
  strip_listing_image_metadata,
  read_accepted_file,
)
from app.manifest_contract import STORE_LISTING_IMAGE_MAX_BYTES


STORE_ART_DIR = "static/store"
# Art the editor writes is named by its content; only those files are the
# listing's own to delete. Other static/store/ files may also be app media.
_EDITOR_ART = re.compile(r"static/store/[0-9a-f]{16}\.(?:png|jpg|webp)")
_DETAILS_NEED_AGENT = (
  "This older app needs an agent to create its app details without "
  "changing its permissions, capabilities, or scheduled jobs."
)


class ListingEditError(ValueError):
  def __init__(self, code: str, message: str, status_code: int = 422):
    super().__init__(message)
    self.code = code
    self.status_code = status_code


@dataclass(frozen=True)
class ListingImage:
  """Either an existing tracked source file or newly uploaded bytes."""

  path: str = ""
  data: bytes | None = None


@dataclass(frozen=True)
class ListingScreenshot:
  image: ListingImage
  alt: str = ""
  label: str = ""


@dataclass(frozen=True)
class ListingEdit:
  tagline: str = ""
  description: str = ""
  screenshots: tuple[ListingScreenshot, ...] = ()
  hero: ListingImage | None = None
  icon: ListingImage | None = None


def record_icon_for_save(root: Path, app: models.App) -> bytes | None:
  """The icon on the app's record that saving a listing copies into source.

  An app from before manifest icons keeps its icon on its record only;
  accepting a revision without one would clear the icon the owner sees.
  """
  try:
    declared = json.loads((root / "mobius.json").read_text(encoding="utf-8")).get("icon")
  except (OSError, ValueError, AttributeError):
    declared = None
  if declared or (root / "icon.png").is_file():
    return None
  return app.icon_override_png or app.icon_png or None


def _icon_cleaned_in_place(declared: str) -> bool:
  """Whether a save rewrites the declared icon file itself, not a copy."""
  return declared.lower().endswith(".png") and not declared.startswith(f"{STORE_ART_DIR}/")


def note_what_saving_fixes(checklist: list[dict], root: Path, app: models.App) -> None:
  """Mark the checklist items that saving the listing completes by itself.

  ``automatic`` is True when a save fixes the item, False when only an agent
  can; absent means the owner (or an agent) supplies it.
  """
  items = {item["id"]: item for item in checklist}
  details = items["details"]
  if not details["done"] and not (root / "mobius.json").is_file():
    details["automatic"] = False
    details["message"] = _DETAILS_NEED_AGENT
  icon = items["icon"]
  # Only a save that can go ahead keeps the icon; one refused for missing or
  # unreadable details, or unapplied source changes, fixes nothing.
  manifest_path = root / "mobius.json"
  manifest = _manifest_from(manifest_path.read_bytes() if manifest_path.is_file() else None)
  if icon["done"] or details.get("automatic") is False or manifest is None:
    return
  if icon.get("code") == "listing_image_metadata":
    declared = str(manifest.get("icon") or "").strip()
    if not _icon_cleaned_in_place(declared):
      # The save writes a clean icon.png, but the original stays published.
      icon["message"] = (
        f"Saving the listing writes a clean icon.png, but {declared} is "
        "published with the source too; ask an agent to remove it or save "
        "it without the metadata."
      )
      return
    message = "Saving the listing removes the icon's hidden metadata."
  elif record_icon_for_save(root, app) is not None:
    message = "Saving the listing keeps the app's current icon."
  else:
    return
  if not has_unapplied_changes(root, app.source_commit):
    icon["automatic"] = True
    icon["message"] = message


def _listing_fields_removed(manifest: dict) -> dict:
  return {key: value for key, value in manifest.items() if key not in {"icon", "store"}}


def _manifest_from(raw: bytes | None) -> dict | None:
  try:
    manifest = json.loads(raw) if raw is not None else None
  except ValueError:
    return None
  return manifest if isinstance(manifest, dict) else None


def _named_by_its_content(rel: str, content: bytes) -> bool:
  return PurePosixPath(rel).stem == hashlib.sha256(content).hexdigest()[:16]


def _clean_editor_art(root: Path, rel: str) -> bool:
  """Whether editor-named art is absent, or a clean image its name hashes."""
  target = root / rel
  if not target.is_file():
    return True
  content = target.read_bytes()
  if not _named_by_its_content(rel, content):
    return False
  try:
    check_listing_image(content, "listing image")
  except CommunityPublicationError:
    return False
  return not listing_image_has_metadata(content)


def _clean_icon(root: Path, rel: str) -> bool:
  """Whether a declared PNG icon path is absent, or a clean listing image."""
  target = root / rel
  if not target.is_file():
    return True
  content = target.read_bytes()
  try:
    check_listing_image(content, "icon")
  except CommunityPublicationError:
    return False
  return not listing_image_has_metadata(content)


def has_unapplied_changes(root: Path, accepted: str | None) -> bool:
  """Whether the source differs from its accepted revision beyond a listing.

  Source with no accepted revision counts as unapplied as a whole. A save
  interrupted after writing, or one that failed after Git advanced,
  leaves listing files changed but not yet accepted; the next save may carry
  them, committed or not. Anything else (code, or manifest fields other than
  the icon and ``store``) was never reviewed and must not be accepted by it.
  """
  if not accepted:
    # With no accepted revision there is nothing to tell reviewed source from
    # unreviewed commits; an apply records one first.
    return True
  changed = app_git.worktree_diff_paths(root, accepted)
  if changed is None:
    return True
  if not changed:
    return False
  manifest_path = root / "mobius.json"
  manifests = [
    _manifest_from(app_git.ref_file(root, accepted, "mobius.json")),
    _manifest_from(manifest_path.read_bytes() if manifest_path.is_file() else None),
  ]
  # A listing save writes only its own hash-named art and a declared PNG icon,
  # so nothing else under the art folder (a script, a hand-placed file) can
  # ride along unreviewed.
  icons = {
    str(manifest.get("icon") or "") for manifest in manifests
    if manifest is not None
    and str(manifest.get("icon") or "").lower().endswith(".png")
  }
  for path in changed:
    if (path in icons and _clean_icon(root, path)) or (
      _EDITOR_ART.fullmatch(path) and _clean_editor_art(root, path)
    ):
      continue
    if path == "mobius.json" and None not in manifests and (
      _listing_fields_removed(manifests[0]) == _listing_fields_removed(manifests[1])
    ):
      continue
    return True
  return False


@dataclass
class _SourceChanges:
  """Original bytes of every path this edit touched, for an exact undo."""

  root: Path
  originals: dict[str, bytes | None] = field(default_factory=dict)

  def _remember(self, rel: str) -> Path:
    target = _source_path(self.root, rel)
    if rel not in self.originals:
      self.originals[rel] = target.read_bytes() if target.is_file() else None
    return target

  def write(self, rel: str, content: bytes) -> None:
    target = self._remember(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)

  def delete(self, rel: str) -> None:
    self._remember(rel).unlink(missing_ok=True)

  def undo(self) -> None:
    for rel, content in self.originals.items():
      target = _source_path(self.root, rel)
      if content is None:
        target.unlink(missing_ok=True)
      else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _bounded(value: str, maximum: int, what: str) -> str:
  value = value.strip()
  if "\x00" in value or len(value.encode("utf-8")) > maximum:
    raise ListingEditError(
      "listing_field_too_long", f"The {what} must be {maximum} UTF-8 bytes or fewer.",
    )
  return value


def _source_path(root: Path, rel: str) -> Path:
  pure = PurePosixPath(rel)
  if (
    not rel or rel.startswith("/") or "\\" in rel or str(pure) != rel
    or any(part in {"", ".", ".."} for part in pure.parts)
  ):
    raise ListingEditError("listing_image_invalid", "That image path is not allowed.")
  try:
    return workspace_files.resolve_path(root, rel, hidden_dirs=workspace_files.GIT_METADATA_NAMES)
  except (workspace_files.InvalidWorkspacePath, workspace_files.UnavailableWorkspacePath) as exc:
    raise ListingEditError("listing_image_invalid", "That image path is not allowed.") from exc


def _accepted_file(root: Path, app: models.App, rel: str) -> bytes:
  """An image kept by path, read from the app's accepted revision.

  The worktree may hold ignored or generated images that were never accepted
  source; copying those into listing art would make them public.
  """
  _source_path(root, rel)
  try:
    content = read_accepted_file(app, rel, max_bytes=STORE_LISTING_IMAGE_MAX_BYTES)
  except CommunityPublicationError as exc:
    if exc.code == "payload_too_large":
      raise ListingEditError(
        "listing_image_too_large",
        f"The image {rel} is larger than "
        f"{STORE_LISTING_IMAGE_MAX_BYTES // (1024 * 1024)} MB.",
      ) from exc
    raise ListingEditError(exc.code, exc.detail, exc.status_code) from exc
  if content is None:
    raise ListingEditError(
      "listing_image_missing",
      f"The image {rel} is not part of the app's applied source. Add it again.",
    )
  return content


def _image_suffix(content: bytes, what: str) -> str:
  try:
    return check_listing_image(content, what)
  except CommunityPublicationError as exc:
    raise ListingEditError(exc.code, exc.detail, exc.status_code) from exc


def _place_art(
  root: Path, app: models.App, changes: _SourceChanges, image: ListingImage, what: str,
) -> str:
  """Return the ``static/store/`` path holding this image, writing it if new.

  Listing art is public, so it never carries camera, location or other
  hidden metadata. New art is content-addressed by the bytes stored, so
  re-saving the same upload is a no-op and reordering screenshots never
  overwrites another one. A kept file outside ``static/store/`` (an older
  listing layout) is copied into it; kept art that still carries metadata
  (an agent committed it directly) is cleaned into a new content-named file.
  """
  content = image.data if image.data is not None else _accepted_file(root, app, image.path)
  suffix = _image_suffix(content, what)
  kept = image.data is None and image.path.startswith(f"{STORE_ART_DIR}/")
  if kept and not listing_image_has_metadata(content):
    rel = image.path
  else:
    content = strip_listing_image_metadata(content)
    suffix = _image_suffix(content, what)
    rel = f"{STORE_ART_DIR}/{hashlib.sha256(content).hexdigest()[:16]}{suffix}"
  target = _source_path(root, rel)
  if not (target.is_file() and target.read_bytes() == content):
    changes.write(rel, content)
  return rel


def _manifest_text(manifest: dict, original: str) -> str:
  """Serialize ``manifest`` in the layout of the file it replaces.

  Keeping the author's indentation, line endings, escaping and final newline
  confines the diff to the listing, so a later Store update does not conflict
  on whitespace. Key order is already kept, since the edit updates the parsed
  dict in place.
  """
  match = re.match(r"\{[ \t]*\r?\n([ \t]+)\S", original)
  indent = match.group(1) if match else ("  " if "\n" in original.strip() else None)
  # Escape non-ASCII only for a file that already does and has no raw form.
  ascii_only = "\\u" in original and original.isascii()
  text = json.dumps(manifest, ensure_ascii=ascii_only, indent=indent)
  if original.endswith("\n"):
    text += "\n"
  return text.replace("\n", "\r\n") if "\r\n" in original else text


def _referenced_art(store: object) -> set[str]:
  if not isinstance(store, dict):
    return set()
  shots = store.get("screenshots")
  paths = [store.get("hero")] + [
    item.get("src") for item in (shots if isinstance(shots, list) else [])
    if isinstance(item, dict)
  ]
  return {
    path for path in paths
    if isinstance(path, str) and path.startswith(f"{STORE_ART_DIR}/")
  }


def _skip_string(text: str, at: int) -> int:
  at += 1
  while text[at] != '"':
    at += 2 if text[at] == "\\" else 1
  return at + 1


def _skip_value(text: str, at: int) -> int:
  if text[at] == '"':
    return _skip_string(text, at)
  if text[at] in "{[":
    depth = 0
    while True:
      if text[at] == '"':
        at = _skip_string(text, at)
        continue
      if text[at] in "{[":
        depth += 1
      elif text[at] in "}]":
        depth -= 1
        if depth == 0:
          return at + 1
      at += 1
  while text[at] not in ",}] \t\r\n":
    at += 1
  return at


def _skip_space(text: str, at: int) -> int:
  while text[at] in " \t\r\n":
    at += 1
  return at


def _top_level_members(text: str) -> tuple[dict[str, tuple[int, int]], str, int]:
  """Each top-level key's value span, the key separator, and the last value's end."""
  at = _skip_space(text, 0) + 1
  spans: dict[str, tuple[int, int]] = {}
  separator, last_end = ": ", at
  while text[at := _skip_space(text, at)] != "}":
    key_end = _skip_string(text, at)
    colon = _skip_space(text, key_end)
    value = _skip_space(text, colon + 1)
    if not spans:
      separator = ":" + text[colon + 1:value].replace("\r", "").replace("\n", "")[:1]
    # The last duplicate wins, as it does for json.loads.
    spans[json.loads(text[at:key_end])] = (value, last_end := _skip_value(text, value))
    at = _skip_space(text, last_end)
    if text[at] == ",":
      at += 1
  return spans, separator, last_end


def _spliced_manifest(original: str, manifest: dict, keys: tuple[str, ...]) -> str:
  """``original`` with only the top-level ``keys`` replaced or added.

  Everything else stays byte for byte, inline arrays and compact styles
  included, so a save's diff is the listing alone. Falls back to a whole
  rewrite in the file's own layout if the result would not parse back.
  """
  try:
    spans, separator, last_end = _top_level_members(original)
    newline = "\r\n" if "\r\n" in original else "\n"
    match = re.match(r"\{[ \t]*\r?\n([ \t]+)\S", original)
    indent = match.group(1) if match else None
    ascii_only = "\\u" in original and original.isascii()

    def value_text(value: object) -> str:
      text = json.dumps(
        value, ensure_ascii=ascii_only, indent=indent,
        separators=("," if indent is not None or separator == ":" else ", ", separator),
      )
      return text.replace("\n", newline + indent) if indent is not None else text

    edits = [
      (*spans[key], value_text(manifest[key])) for key in keys if key in spans
    ]
    added = "".join(
      ("," + newline + indent if indent is not None else ("," if separator == ":" else ", "))
      + json.dumps(key) + separator + value_text(manifest[key])
      for key in keys if key not in spans
    )
    if added:
      edits.append((last_end, last_end, added))
    text = original
    for start, end, replacement in sorted(edits, reverse=True):
      text = text[:start] + replacement + text[end:]
    if json.loads(text) == manifest:
      return text
  except Exception:  # Any surprise in the splice falls back to a rewrite.
    pass
  return _manifest_text(manifest, original)


def _icon_with_metadata(root: Path, declared: str) -> bytes | None:
  """The declared icon's bytes if it is a listing image with hidden metadata."""
  if not declared:
    return None
  try:
    content = _source_path(root, declared).read_bytes()
    check_listing_image(content, "app icon")
    return content if listing_image_has_metadata(content) else None
  except (OSError, ListingEditError, CommunityPublicationError):
    return None


def write_listing(root: Path, app: models.App, edit: ListingEdit) -> Callable[[], None]:
  """Write ``edit`` into the app source at ``root`` and return its undo."""
  if len(edit.screenshots) > MAX_STORE_SCREENSHOTS:
    raise ListingEditError(
      "listing_too_many_screenshots",
      f"Keep at most {MAX_STORE_SCREENSHOTS} screenshots.",
    )
  tagline = _bounded(edit.tagline, TAGLINE_MAX_BYTES, "tagline")
  description = _bounded(edit.description, DESCRIPTION_MAX_BYTES, "description")
  changes = _SourceChanges(root)
  try:
    manifest_path = _source_path(root, "mobius.json")
    if manifest_path.is_file():
      try:
        # Bytes, not text mode, which would fold CRLF line endings away.
        raw = manifest_path.read_bytes()
        # A byte-order mark is kept as it was; JSON itself never holds one.
        bom = codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b""
        original = raw[len(bom):].decode("utf-8")
        current = json.loads(original)
      except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ListingEditError(
          "manifest_invalid", "mobius.json is not valid JSON. Ask an agent to repair it.",
        ) from exc
      if not isinstance(current, dict):
        raise ListingEditError("manifest_invalid", "mobius.json must be an object.")
    else:
      raise ListingEditError("details_need_agent", _DETAILS_NEED_AGENT, 409)

    declared_icon = str(current.get("icon") or "").strip()
    icon_rel = declared_icon or "icon.png"
    icon_bytes = None
    if edit.icon is not None:
      icon_bytes = (
        edit.icon.data if edit.icon.data is not None
        else _accepted_file(root, app, edit.icon.path)
      )
    elif (declared := _icon_with_metadata(root, declared_icon)) is not None:
      # The icon is published as it is; normalizing it drops hidden metadata.
      icon_bytes = declared
    else:
      icon_bytes = record_icon_for_save(root, app)
    if icon_bytes is not None and declared_icon:
      # Normalized icons are PNG; never write PNG bytes under another suffix,
      # nor over content-addressed listing art the icon may share a file with.
      if not _icon_cleaned_in_place(icon_rel):
        icon_rel = "icon.png"
    if icon_bytes is not None:
      try:
        changes.write(icon_rel, icon_assets.normalize_icon(icon_bytes))
      except icon_assets.InvalidIcon as exc:
        raise ListingEditError("listing_image_invalid", f"App icon: {exc}") from exc
    has_icon = _source_path(root, icon_rel).is_file()

    screenshots = []
    for index, shot in enumerate(edit.screenshots, start=1):
      entry = {
        "src": _place_art(root, app, changes, shot.image, f"screenshot {index}"),
        "alt": _bounded(shot.alt, SCREENSHOT_ALT_MAX_BYTES, f"screenshot {index} description"),
      }
      label = _bounded(shot.label, SCREENSHOT_LABEL_MAX_BYTES, f"screenshot {index} caption")
      if label:
        entry["label"] = label
      screenshots.append(entry)
    # Fields the editor does not manage (a curator's flag, a future field)
    # stay exactly as they were; assigning existing keys keeps their order.
    previous = current.get("store")
    store: dict[str, object] = dict(previous) if isinstance(previous, dict) else {}
    store["tagline"] = tagline
    store["description"] = description
    if edit.hero is not None:
      store["hero"] = _place_art(root, app, changes, edit.hero, "banner image")
    else:
      store.pop("hero", None)
    store["screenshots"] = screenshots

    previous_art = _referenced_art(current.get("store"))
    manifest = dict(current)
    if has_icon:
      manifest["icon"] = icon_rel
    manifest["store"] = store
    changes.write(
      "mobius.json",
      bom + _spliced_manifest(
        original, manifest, tuple(key for key in ("icon", "store") if key in manifest),
      ).encode("utf-8"),
    )

    # Listing art the icon still uses is not stale, and only the editor's own
    # content-addressed files are removed.
    for stale in previous_art - _referenced_art(store) - {manifest.get("icon")}:
      # The editor's own files hold the bytes their names hash; an author's
      # file that merely looks like one is left alone.
      target = root / stale
      if not (
        _EDITOR_ART.fullmatch(stale) and target.is_file()
        and _named_by_its_content(stale, target.read_bytes())
      ):
        continue
      changes.delete(stale)
  except BaseException:
    changes.undo()
    raise
  return changes.undo
