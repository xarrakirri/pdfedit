# pdfedit — PDF editing at the document-object level

*[Русская версия](README.ru.md)*

pdfedit replaces text **directly inside PDF content streams** — it rewrites the
operands of the `Tj` and `TJ` text-showing operators. It does not paint patches
over the page and it does not rasterise the document. Embedded fonts, sizes,
colours, spacing, bookmarks, form fields and metadata all stay where they were.

Intended uses: correcting typos in official documents, working with archives
where metadata must remain untouched, and testing PDF-processing pipelines.

> Built in collaboration with an LLM and driven to a working state iteratively:
> 225 automated tests, validated against 60 real-world documents.
> Everything that does not work is listed honestly under
> [Limitations](#limitations) — including cases that are unsolvable in principle.

> **A note on output samples.** The command-line interface currently prints its
> reports in Russian. Sample output in this document has been translated for
> readability; the wording you see on screen will differ. Localising the CLI is
> an open task and a good first contribution.

---

## Contents

- [What it does](#what-it-does)
- [Installation](#installation)
- [macOS application](#macos-application)
- [Quick start](#quick-start)
- [Command line](#command-line)
- [Graphical mode](#graphical-mode)
- [How it works](#how-it-works)
- [Width-fitting modes](#width-fitting-modes)
- [Working with fonts](#working-with-fonts)
- [Donor font library](#donor-font-library)
- [Metadata](#metadata)
- [Three saving modes](#three-saving-modes)
- [Encrypted documents](#encrypted-documents)
- [Digital signatures](#digital-signatures)
- [Structural validation](#structural-validation)
- [Leaving no editing traces](#leaving-no-editing-traces)
- [Programmatic interface](#programmatic-interface)
- [Tests](#tests)
- [Limitations](#limitations)
- [Source layout](#source-layout)

---

## What it does

| Capability | Status |
|---|---|
| Text replacement in `Tj`/`TJ` operators | yes |
| Embedded fonts and their glyphs preserved | yes |
| Missing glyphs added to an embedded font | yes, for TrueType |
| Fallback font embedded when the original will not do | yes |
| Cyrillic and any other script | yes |
| Text inside Form XObjects (stamps, letterheads) | yes |
| Composite Type0/CID fonts (Identity-H) | yes |
| Simple Type1/TrueType fonts, encodings and `/Differences` | yes |
| Non-embedded standard fonts | yes, with a warning |
| `/Info` metadata editing with XMP synchronisation | yes |
| Original `/ID`, version and structure preserved | yes |
| Incremental save: original bytes and object numbers unchanged | yes, `--incremental` |
| In-place stream editing: file length and other object hashes unchanged | yes, `--inplace` |
| Editing an encrypted document with encryption preserved | yes, when appending (RC4, AES-128, AES-256) |
| Structural integrity check and object-tree diff against the original | yes, `check` |
| Graphical editing of text directly on the rendered page | yes |
| Type3 fonts | read-only |
| Scanned documents (no text layer) | not applicable |
| Applying a digital signature | no (see [Digital signatures](#digital-signatures)) |

---

## Installation

Requires Python 3.9 or newer.

```bash
git clone https://github.com/xarrakirri/pdfedit && cd pdfedit
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Dependencies:

| Package | Purpose |
|---|---|
| `pikepdf` (≥ 8) | access to PDF objects, parsing and assembling content streams |
| `PyMuPDF` (≥ 1.24) | page rendering for the graphical mode, standard font metrics |
| `fonttools` (≥ 4.40) | reading font programs, adding glyphs, subsetting |

Tkinter ships with the standard library. Some Linux distributions package it
separately: `sudo apt install python3-tk`.

No cryptographic libraries are needed: encryption of appended objects
(RC4, AES-128, AES-256) is implemented in `pdfedit/pdfcrypt.py` on top of
`hashlib` from the standard library. Everything runs locally and no file is ever
sent anywhere — neither during editing nor during validation.

Verify the installation:

```bash
python -m pdfedit --version
```

Sample documents to experiment on:

```bash
python samples/make_samples.py
```

---

## macOS application

The program can be built into a regular application — with its own icon, its own
name in the menu bar, a place in the Dock and an association with PDF files.

```bash
python app/build_app.py --install
```

The finished application lands in `~/Applications/pdfedit.app`, with a copy in
`dist/`. From then on it lives its own life: double-click to launch, drag a PDF
onto the icon, or use "Open With".

```bash
open ~/Applications/pdfedit.app                     # just launch it
open -a ~/Applications/pdfedit.app contract.pdf     # launch with a document
```

**The application is self-contained.** The bundle holds everything: the Python
interpreter with its standard library, `pikepdf`, `PyMuPDF`, `fontTools` and the
`pdfedit` package itself — roughly 145 MB. After the build, the source tree, the
virtual environment and even Python itself are no longer required on the machine;
the bundle can be moved anywhere or handed to another computer.

Build modes:

| Command | Result |
|---|---|
| `python app/build_app.py` | self-contained application in `dist/` |
| `python app/build_app.py --install` | the same, plus a copy in `~/Applications` |
| `python app/build_app.py --mode thin` | a thin wrapper around the development tree: builds instantly, but breaks if the tree moves |
| `python app/build_app.py --no-sign` | skip local signing |

The icon is drawn programmatically (`app/make_icon.py`) — a sheet of paper with
the line being edited highlighted. No image file is needed.

**What the build does, and why.** Three things, without which the application
would misbehave:

- *Program name.* On macOS the framework interpreter hands control to a nested
  `Python.app` bundle — only a bundled application gets access to the window
  server. The system takes the menu-bar name, the Dock name and the process name
  from that bundle, so the build rewrites its `Info.plist` and renames the
  interpreter binary. Without this the user would simply see "Python".
- *Architecture.* The system `python3` is a universal binary, and when launched
  from Finder macOS may well pick x86_64 while the binary extensions were built
  for this machine's architecture. The launch script pins the architecture
  explicitly.
- *Diagnostics.* Output from a program started via Finder normally vanishes
  without trace. Here it is written to `~/Library/Logs/pdfedit.log`, and on a
  crash a system dialog shows the last lines of that log.

The application is signed locally ("ad-hoc"). This is not a developer signature:
on another machine macOS will ask for confirmation on first launch (right-click →
"Open", or System Settings → Privacy & Security).

On other platforms no bundle is built — the program runs as `python -m pdfedit gui`.

---

## Quick start

```bash
python -m pdfedit replace contract.pdf -o contract-fixed.pdf --old "Smith" --new "Jones"
```

Inspect the document to see what is inside and where to take search strings from:

```bash
python -m pdfedit inspect contract.pdf
```

Open the graphical editor and edit text directly on the page:

```bash
python -m pdfedit gui contract.pdf
```

---

## Command line

Five commands: `inspect`, `replace`, `meta`, `verify`, `gui`.
Each has a `--help` with the full list of options.

### `inspect` — what is inside the document

```bash
python -m pdfedit inspect contract.pdf --pages 1-2
```

Prints metadata, the font list with an embedding note, and every text run with
its coordinates:

```
File: contract.pdf
Pages: 2; version 1.7, object streams: no, linearised: no, XMP: yes

/Info dictionary:
  /Author       = John Q. Smith
  /CreationDate = D:20210305093000+03'00'
  ...

Fonts (1):
  /F0 | /Times New Roman Regular | /Type0 | embedded: truetype

Text runs (10):
  p.1 #1 /F0 16pt (72,738)-(347,756)
      'AGREEMENT No. 17-A of 5 March 2021'
```

### `replace` — text replacement

```bash
# a single replacement
python -m pdfedit replace in.pdf -o out.pdf --old "Acme" --new "Globex"

# several in one pass (--old and --new come in pairs)
python -m pdfedit replace in.pdf -o out.pdf \
    --old "Acme"     --new "Globex" \
    --old "150 000"  --new "200 000"

# see what would be replaced, changing nothing
python -m pdfedit replace in.pdf -o out.pdf --old "2021" --new "2022" --dry-run

# regular expression with group references
python -m pdfedit replace in.pdf -o out.pdf --regex \
    --old "No. (\d+)-A" --new "No. \1-B"

# first occurrence only, pages 1 and 3-5 only, case-insensitive
python -m pdfedit replace in.pdf -o out.pdf --old "smith" --new "Jones" \
    --count 1 --pages 1,3-5 --ignore-case

# replacement together with a metadata edit
python -m pdfedit replace in.pdf -o out.pdf --old "2021" --new "2022" \
    --set author="J. Jones" --set moddate="2022-04-12 10:00:00"
```

Main options:

| Option | Meaning |
|---|---|
| `--old` / `--new` | what to replace and with what; given in pairs |
| `--edits FILE.json` | apply an edit list exported from the graphical mode |
| `--count N` | replace only the first N occurrences (0 — all) |
| `--regex`, `--ignore-case`, `--whole-word` | search modes |
| `--pages 1,3-5` | restrict to pages |
| `--fit MODE` | how to fit the width, see [below](#width-fitting-modes) |
| `--set FIELD=VALUE`, `--del FIELD` | metadata editing |
| `--touch-moddate` | set the current modification date (by default it is left alone) |
| `--no-font-extension` | do not add glyphs to embedded fonts |
| `--no-fallback-font` | do not embed a fallback font |
| `--font-dir DIR` | additional place to look for donor fonts |
| `--new-id` | generate a new `/ID` instead of preserving the original |
| `--password` | password for an encrypted document |
| `--dry-run` | only report what was found |

After saving, the program compares the result against the original by itself and
prints a report.

### `meta` — metadata only

```bash
# view
python -m pdfedit meta document.pdf --show

# change author and creation date, remove the modification date
python -m pdfedit meta in.pdf -o out.pdf \
    --set author="J. Q. Smith" \
    --set created="2019-01-01 10:00:00" \
    --del moddate --show
```

Field names are accepted loosely: `author`, `Author`, `/Author`; `created`,
`creationdate`; `moddate`.

Date formats:

| Form | Example |
|---|---|
| human-readable | `2021-03-05 12:00:00`, `2021-03-05` |
| ISO 8601 | `2021-03-05T12:00:00+03:00` |
| native PDF format | `D:20210305120000+03'00'` |
| current moment | `now` |

### `verify` — compare the result against the original

```bash
python -m pdfedit verify original.pdf result.pdf
```

```
OK /Info dictionary: no unintended changes
OK XMP metadata: no unintended changes
OK /ID preserved
OK PDF version matches
OK page count matches
```

Exit code 0 — no discrepancies; 2 — discrepancies found.

<a id="check-command"></a>

### `check` — structural integrity and an object-by-object diff

```bash
# validate the file only
python -m pdfedit check result.pdf

# and diff against the original: what exactly changed in the object tree
python -m pdfedit check result.pdf --original original.pdf
```

```
File: result.pdf
     version 1.5, 2 pages, 31 objects (12 streams), 2 revisions in file
     3 fonts, 4 images, 1 annotation
     /Info: yes, XMP: no, /ID: yes
OK structural violations: 0

Verdict: structure intact

Diff: original.pdf -> result.pdf
OK page count matches
OK unintended changes in the object tree: 0
     changed by the text edit itself (expected): 2
       page 1/Resources/Font/F1/DescendantFonts[0]/FontDescriptor/FontFile2/Length1: 20372 -> 20456
       page 1/Resources/Font/F1/DescendantFonts[0]/W: array length 84 -> 86
OK images: identical
OK annotations in place
OK metadata untouched
OK /ID preserved
OK original file bytes preserved (yes)
OK object numbers preserved (yes)
```

The `--strict` flag answers two questions that integrity alone does not cover.

**First: what gives the edit away under inspection** (`pdfedit/traces.py`). A
document can be perfectly intact and contain exactly the intended changes — and
still carry the marks of another hand. Some are visible in the file itself, some
only in comparison with the original:

```
Editing traces — what the file reveals under inspection:
No traces found. Checked:
  OK no unused bytes after the compressed data in any stream
  OK widths in the dictionary match hmtx (fonts checked: 1)
  OK no orphan glyphs in subset fonts (checked: 1)
  OK hidden text copies match the page content
  OK strings written consistently across all streams
  OK file consists of a single revision

Traces visible when compared with the original:
No traces found. Checked:
  OK /ID unchanged
  OK /Producer and /Creator untouched
  OK XMP metadata untouched
  OK /CreationDate and /ModDate untouched
  OK object numbering preserved (6 objects)
  OK font timestamps (head.modified) untouched
  OK table order inside fonts preserved
  OK stream compression level unchanged
  OK file length unchanged
  OK cross-reference style unchanged (xref table)
```

What is actually looked for:

| marker | what it means |
|---|---|
| `stream-tail` | unused bytes sit after the compressed data — an in-place edit was padded out |
| `compression-level` | the zlib header changed: the stream was recompressed by different software |
| `inconsistent-writing` | one string is written differently from every other string in the stream |
| `hidden-copy` | `/ActualText` disagrees with the page text |
| `orphan-glyph` | a subset font contains a glyph no code refers to |
| `width-mismatch` | `/W` (`/Widths`) disagrees with the font's `hmtx` table |
| `font-date` | `head.modified` changed — the font was rebuilt |
| `table-order` | tables inside the font sit in a different order |
| `hinting` | `fpgm`, `prep` or `cvt` changed |
| `id-changed`, `date-changed`, `producer-changed`, `xmp-changed` | something was touched that should not have been |
| `new-objects`, `objects-vanished` | object numbering drifted |
| `xref-style`, `version`, `file-length` | the container's presentation changed |

The checks are independent: one failing on an unusual document does not cancel
the rest — it goes into the report as its own line, because silence would be read
as "no traces".

**Second: why external validators would reject the document** — veraPDF, Acrobat
Preflight, ingestion systems. A file can be intact and free of editing traces and
still fail these, because they ask about profile conformance (most often PDF/A).
Together with `--original`, the findings are split into two lists — and that split
is the point:

```bash
python -m pdfedit check out.pdf --original in.pdf --strict
```

```
Findings from strict external validators (PDF/A, ingestion systems):
  -- present in the original too:  no XMP metadata (/Metadata) — PDF/A requires it
  -- present in the original too:  no /OutputIntents with a colour profile — required for PDF/A
  -- present in the original too:  fonts without /ToUnicode: /SRSXRV+ALSRubl
  -- present in the original too:  transparency is used — PDF/A-1 forbids it

  All findings are inherited from the source file: the edit added none of them.
```

Checked: encryption, absence of XMP and `pdfaid:part`, absence of
`/OutputIntents`, absence of `/ID`, non-embedded fonts, fonts without
`/ToUnicode`, transparency, and the number of revisions in the file. That last
one is the only item the edit itself can introduce: `--incremental` adds a second
revision, and ingestion systems sometimes dislike that. Full rebuild and
`--inplace` both leave the document as a single revision.

What is checked in the file itself: whether the document opens, whether there are
dangling references, whether every stream decompresses, whether page content
parses, whether font programs are intact, and the state of annotations and digital
signatures. qpdf parse complaints (`parse error`, `EOF while reading token`) count
as violations: qpdf silently recovers from content-parsing problems, and without
treating them as violations a corrupted page stream would pass unnoticed.

The diff walks both documents simultaneously from the root and names each
discrepancy by its path inside the tree rather than by object number — numbers
change on a full rebuild, paths do not. Changes caused by the text edit itself
(page content, font programs, `/W`, `/ToUnicode`, `/ActualText`, a font added to
the resources) are shown separately from unintended ones.

Exit code 0 — file intact and no unintended changes; 2 — otherwise.

---

## Graphical mode

```bash
python -m pdfedit gui contract.pdf
```

Or, if the [application](#macos-application) has been built, by double-clicking
`pdfedit.app` or the PDF itself via "Open With".

The window shows the real rendered page. Only the visible portion is rendered, so
memory use depends on neither zoom level nor page size: scanned documents can run
to 1900×2800 points, and drawing one whole at 3× zoom would need around half a
gigabyte — at which point the system kills the process without warning. Text is
edited **directly on the page**:

1. editable runs are outlined in blue;
2. clicking a run opens an input field exactly in its place;
3. `Enter` applies the edit, `Esc` cancels;
4. the page is immediately redrawn **from the already-modified document** — you
   see the actual result, not a preview sketch;
5. changed runs are highlighted green, failed ones red.

Highlight colours:

| Colour | Meaning |
|---|---|
| blue outline | the run can be edited |
| grey outline | the run is not editable (a Type3 font, for instance) |
| green fill | the run has been changed |
| red outline | the edit could not be applied; the reason is in the log |

The right-hand panel:

- **Edits** — the list of changes made; double-click jumps to the relevant page,
  and "Export…" saves them as JSON for `replace --edits`;
- **Metadata** — the `/Info` fields; an empty field means "leave as it was";
- **Log** — warnings, notes about font modifications, the verification report.

The "Text width" dropdown in the toolbar switches the fitting mode with an
immediate redraw, so the options can be compared by eye.

Menu bar: File (open, save, import and export the edit list), Edit, View, Help.
Closing the window with unsaved edits raises a warning and offers to save.

Shortcuts: `⌘O` open, `⌘S` save as, `⌘+`/`⌘−` zoom, `PageUp`/`PageDown` pages,
`Ctrl` + wheel zoom. On Windows and Linux, `Ctrl` replaces `⌘`.

---

## How it works

A PDF content stream contains no text — it contains **glyph codes of a specific
font**. The string `(\x02>\x02h\x02]) Tj` might mean "Agr", or anything else
entirely: the font decides what the codes mean. Replacement therefore proceeds in
five steps.

**1. Parsing the stream.** `pikepdf.parse_content_stream` yields a list of
operators. The program walks them while tracking the full graphics state: the
transformation matrix (`cm`, `q`/`Q`), the text matrices `Tm`/`Tlm`, font size,
character and word spacing, horizontal scaling `Tz`, text rise `Ts` and fill
colour. Nested Form XObjects are parsed recursively.

**2. Decoding.** For each font a "code → character" table is built: from
`/ToUnicode`, from `/Encoding` with `/Differences`, with any gaps filled in from
the `cmap` of the embedded font program itself. Adjacent glyphs are grouped into
**text runs** — visually coherent pieces sharing a font, size and colour. Every
character remembers which position of which operator it came from.

**3. Searching.** The search runs over run text with normalisation: a non-breaking
space is treated as an ordinary one, the various dashes as a hyphen, the `ﬁ`
ligature as `fi`. The matched character range is then translated back into a glyph
range.

**4. Encoding the new text.** The "code → character" table is inverted. The key
subtlety: a code having an entry in `/ToUnicode` does not mean the glyph exists in
the font — subsetting tools frequently leave the table intact while gutting the
glyphs themselves. Glyph presence is therefore verified against the font program,
not against the table. If a glyph is missing, the font is
[extended](#working-with-fonts).

**5. Reassembling the operator.** The operands are decomposed into "atoms" —
individual glyphs and kerning numbers. Atoms being replaced are removed, the new
bytes are inserted in their place, and neighbours are merged back into strings.
The width difference is compensated according to the
[chosen mode](#width-fitting-modes). If the result is a single string with no
numbers, a compact `Tj` is written — keeping the stream as close to the original
as possible.

Geometry is verified for correctness: the run rectangles computed by the program
match the coordinates from an independent parser (PyMuPDF) to within fractions of
a point — that is a test of its own.

---

## Width-fitting modes

New text almost never matches the old text in width. What to do with the
difference is chosen by `--fit`.

| Mode | Behaviour | When to use it |
|---|---|---|
| `auto` (default) | a difference under 8 % is hidden by horizontal scaling; a larger one reflows the line | ordinary typos |
| `natural` | the line reflows and the tail shifts | edits where the layout may breathe |
| `preserve` | following text stays exactly in place via a numeric correction in `TJ` | tables and columns where shifting is unacceptable |
| `squeeze` | the new text is scaled by `Tz` to exactly the original width | form fields of fixed width |

In practice — replacing "5 March 2021" with "12 September 2022" in the line
"… dated 5 March 2021 of the year":

- `natural` — "of the year" moves further right;
- `preserve` — "of the year" stays put, but the new text overlaps it, being longer;
- `squeeze` — "of the year" stays put and the new text is fitted into the original width;
- `auto` — picks `squeeze` for a small difference, `natural` otherwise.

`preserve` genuinely keeps the position of all following text, but with a large
width difference that means overlap — it is the mode for cases where shifting is
worse than overlapping.

---

## Working with fonts

When the font lacks the required glyphs (the common case: a document typeset in
Latin script into which Cyrillic is being inserted), the program tries three
things in order:

**1. Extending the embedded subset.** Outlines for the missing glyphs are taken
from a system font of the same family and appended to the embedded program.
Composite glyphs are decomposed into simple ones (otherwise the references to the
donor's glyph indices would be wrong), and outlines are scaled if the units per em
differ. `/W` or `/Widths`, `/Differences`, `/CIDSet` and `/ToUnicode` are all
updated. The result is outwardly indistinguishable from the original set: the same
outlines, the same metrics.

```
font: Times New Roman Regular: glyphs ' «»ABCDEFG' added from "Times New Roman"
```

For simple single-byte fonts, new characters are assigned codes that no character
in the document uses — redefining those through `/Differences` is safe.

**2. Embedding a fallback font.** If no donor of the same family exists, the
program format cannot be edited (CFF/Type1), or the font was never embedded at
all, a new Type0 font is added to the document and used to typeset the changed run
only. `Tf` operators restoring the original font are placed around the insertion,
so the surrounding text is unaffected.

**3. Refusal.** If both are forbidden (`--no-font-extension --no-fallback-font`),
the edit is skipped with an explanation.

Fonts are looked up in the system directories (`/System/Library/Fonts`,
`/Library/Fonts`, `C:/Windows/Fonts`, `/usr/share/fonts` and so on); the index is
cached in `~/.cache/pdfedit/fontindex.json`. Additional directories are given with
`--font-dir`.

**Non-embedded fonts.** If a font is not embedded, its glyphs are drawn by the
reader's system. The program warns about this and by default embeds a fallback
font so that the document looks the same everywhere.

---

## Donor font library

A document does not carry the whole font, only the glyphs it actually used. Set in
Latin script, it contains no Cyrillic, and there is nothing to insert it with. The
missing letters have to come from outside, and there are three sources; the program
tries them in this order:

1. **Other fonts in the same document.** The same typeface is often embedded
   several times as different subsets: the headings contain letters the body text
   does not. These are literally the same outlines — no better source exists. This
   works automatically; disable it with `--no-document-fonts`.
2. **A personal library** assembled from other PDFs — for example, from documents
   by the same publisher that do contain the needed characters.
3. **System fonts** — matched by family, weight and style.

Adding to the library:

```bash
python -m pdfedit fonts add document-with-the-right-letters.pdf
python -m pdfedit fonts list
python -m pdfedit fonts remove Times
python -m pdfedit fonts clear
```

See what a file has embedded, without adding anything anywhere:

```bash
python -m pdfedit fonts show document.pdf
```

```
  + TimesNewRomanPS-BoldMT     truetype   characters: 77
  + TimesNewRomanPSMT          truetype   characters: 121
  - Times-Bold                 none       font not embedded in the document — nothing to take
```

In the graphical mode the same lives under the Fonts menu: importing from a PDF,
browsing the library, and toggles for the sources.

**Rebuilding the character map.** A subset extracted from a PDF almost always
arrives without a `cmap` table: rendering the page does not need one, since glyph
codes are written straight into the content stream. A donor, however, requires it —
otherwise the font cannot answer whether it has a given letter. The program
reconstructs that table from the document's own data (`/ToUnicode` and the
encoding), after which the extracted font becomes an ordinary self-sufficient font
file.

Limitation: Type1 and bare CFF fonts are not supported as donors — their programs
are not standalone font files. Such entries are flagged in the output, and
replacement for them goes through system fonts.

The library lives in `~/Library/Application Support/pdfedit/fonts` (macOS),
`~/.local/share/pdfedit/fonts` (Linux) or `%APPDATA%\pdfedit\fonts` (Windows). The
directory can be redirected with the `PDFEDIT_FONT_LIBRARY` environment variable.

---

## Metadata

Metadata lives in two places in a PDF, and the two are required to agree: the
`/Info` dictionary in the trailer and the XMP stream in `/Root /Metadata`. A
disagreement between them is the first thing document-inspection tools point at, so
any edit synchronises the values:

| `/Info` field | XMP property |
|---|---|
| `/Title` | `dc:title` |
| `/Author` | `dc:creator` |
| `/Subject` | `dc:description` |
| `/Keywords` | `pdf:Keywords` |
| `/Creator` | `xmp:CreatorTool` |
| `/Producer` | `pdf:Producer` |
| `/CreationDate` | `xmp:CreateDate` |
| `/ModDate` | `xmp:ModifyDate` |

The `--xmp` option controls the behaviour: `auto` — update the existing stream
(default), `always` — create one if absent, `never` — leave it alone.

---

## Three saving modes

Saving has three distinct goals, and choosing between them has to be deliberate.

| | **Full rebuild** (default) | **Incremental** (`--incremental`) | **In-place** (`--inplace`) |
|---|---|---|---|
| Original file bytes | rewritten | left untouched | changed only inside edited streams |
| File length | different | larger by the size of the layer | **the same** |
| Object numbers | qpdf renumbers them | unchanged | unchanged |
| Unchanged objects | rewritten (same content) | untouched | **byte-for-byte identical by hash** |
| `/ID`, `/Info`, dates | restored after writing | untouched | never physically rewritten |
| `/Encrypt`, password, permissions | dropped unless asked otherwise | preserved as-is | preserved as-is |
| Revision count (`%%EOF`) | one | one more than before | **unchanged** |
| Previous text remains in the file | no | yes, in the old revision | no, overwritten |
| When applicable | always | always | when the edit fits the original stream length |

In-place editing is the most conservative option and a direct answer to the
question "is this still the same file?": a hash comparison shows that exactly one
object changed.

```bash
python -m pdfedit replace in.pdf -o out.pdf --old Smith --new Jones --inplace
```

```
In-place edit:
objects written in place: 1 (2 bytes of padding)
     12 0 R
file length: unchanged
```

```bash
python -m pdfedit check out.pdf --original in.pdf --hashes
```

```
Object hashes: 27 of 28 match byte for byte
OK objects vanished: 0
     changed: 1
       12 0 R — page 1 content: data changed
           instruction #37: "Total" Tj  ->  "Tota" Tj
OK file bytes: length unchanged, 802 bytes differ — this is the in-place stream edit
```

The report names not only the object number but its role in the document
("page 1 content", "font program X", "`/ToUnicode` table of font X", "image on
page 3") and breaks down the change itself: for content, which instructions
diverged, with the text decoded through the encoding of whatever font was in
effect at that point. For edits that required new glyphs it looks like this:

```
     changed: 4
       12 0 R — page 1 content: data changed
           from instruction #37: was "Total" Tj
                                 now 102.717 Tz | "Tota" Tj | 100 Tz
       19 0 R — font program /UUUPYY+TinkoffSans-Medium: dictionary and data changed
           key /Length1: 5144 -> 5296
           data: 5144 -> 5296 bytes
       21 0 R — descendant of font /UUUPYY+TinkoffSans-Medium: dictionary changed
           key /W: array 14 -> 16 elements
       22 0 R — /ToUnicode table of font /UUUPYY+TinkoffSans-Medium: data changed
           data: 447 -> 434 bytes
```

How this works. The stream data is written over the old data, and the difference in
length is made up with padding: the Flate decoder stops at the end of the
compressed data and never reads the tail beyond it, so `/Length` stays the same,
and with it every object offset, the cross-reference table and the trailer. For the
new data to fit at all, the stream is not reassembled wholesale: changed
instructions are substituted directly into the original bytes
(`pdfedit/streampatch.py`), while untouched ones remain the very same bytes. A full
reassembly would reliably add 2–4 % of length purely from different formatting —
and the edit would stop fitting.

When an in-place edit does not work out (the text grew longer, the stream
dictionary changed, the object lives in a compressed `/ObjStm`, the stream has
`/DecodeParms`), it falls back: such objects are appended as a layer, and the
result remains correct. The report shows exactly what did not fit and why. On a
test set of 59 files, a "remove one letter from a word" edit landed fully in place
in 41 files; in the rest, part went into the layer.

A separate limitation: pages whose `/Contents` is an array of several streams are
not handled in place. The editor parses such an array as one concatenated content
and, on writing, collapses it into the first stream, changing the page dictionary;
that is inexpressible in place. In the test set, 2 files out of 63 are like this.

Incremental saving is a standard PDF mechanism (ISO 32000-1, 7.5.6): new revisions
of only the changed objects, plus a new cross-reference table with a `/Prev` link to
the previous one, are appended to the end of the file. The reader reads the last
table, takes the new offsets of the changed objects from it, and reads everything
else — the streams of untouched pages, fonts, images, annotations, the structure
tree — from the original part of the file. That part is physically the same, so
corrupting it is impossible in principle.

```bash
python -m pdfedit replace in.pdf -o out.pdf --old Smith --new Jones --incremental
```

The post-save report shows exactly what was appended:

```
Appended edit layer:
objects appended: 4 (4 changed, 0 added)
cross-reference: xref
file growth: 15619 bytes
new revisions: 8 0 R, 16 0 R, 18 0 R, 19 0 R
```

The type of the new cross-reference always matches the old one: a classic `xref`
table gets a table, an `/XRef` stream gets a stream. Otherwise a reader that could
read the document before the edit would meet an unfamiliar construct. An object
that lived inside a compressed `/ObjStm` is moved out as an ordinary object when
rewritten — the specification permits this.

Before writing, the result is re-read and compared against the in-memory document:
the original bytes must still be in place, the page count must match, and every
appended object must read back as exactly what it was. If anything disagrees, the
file is not written at all.

What incremental saving does not do:

- **Linearisation ("fast web view") stops being trustworthy** — a warning is
  issued. This does not affect reading, but a strict linearisation check will point
  at it.
- **The previous text stays in the file.** The old revision of the stream does not
  go anywhere — it sits in the original part. That is the price of leaving the
  original inviolate: if you need a file with no trace of the edit, you need the
  full-rebuild mode.

---

## Encrypted documents

A document with `/Encrypt` is opened with a password (`--password`), but saving
works differently depending on the mode.

**Incremental saving preserves encryption completely.** The `/Encrypt` dictionary
stays as it was — along with the owner password, the permissions and the algorithm.
Appended objects are encrypted with the same document key: the key comes from qpdf,
the object key is derived by Algorithm 1 of ISO 32000-1 (MD5 of the file key with
the object number and generation, plus the `sAlT` marker for AES), and the cipher
is RC4 or AES-CBC. `/V` 1–5 are supported, that is RC4 40–128 bit, AES-128
(`/AESV2`) and AES-256 (`/AESV3`). AES is implemented in the program itself
(`pdfedit/pdfcrypt.py`), requiring no external crypto libraries; correctness is
verified against the FIPS-197 test vectors.

**A full rebuild drops encryption by default** — with a warning. The
`--keep-encryption` option reproduces it: the same algorithm, the same key length,
the same permissions. But two things cannot be reproduced, and the program says so
plainly:

- **the owner password.** The file does not store it as such, only a verification
  value `/O` from which the original password cannot be recovered. You can set your
  own with `--owner-password`; otherwise it will be empty, and the permissions,
  while formally present, could be lifted by anyone;
- **`/ID`.** The encryption key is derived from it, so `/ID` cannot be substituted
  in an already-written encrypted file — the document would stop opening.

```bash
python -m pdfedit replace encrypted.pdf -o out.pdf --old 2021 --new 2022 \
    --keep-encryption --owner-password 'MySecret1'
```

---

## Digital signatures

Any text edit invalidates a signature — that is precisely what a signature is for.
There is no way around it: the signature covers the document's bytes
cryptographically, and no method of writing will make a verifier say "signature
valid" again for changed content. The program does not attempt it.

What can actually be chosen is *how* the fact of the edit becomes visible:

- **Incremental (`--incremental`).** The signed revision stays in the file byte for
  byte, so the signature remains mathematically verifiable *for its own revision*:
  the `/ByteRange` is intact and the hash checks out. Acrobat and other readers show
  such a document as "signed, document has been modified since signing" and let you
  view the signed version. This is exactly how every tool that adds annotations or
  countersignatures to a signed document works.
- **Full rebuild.** The signature becomes a meaningless object: the bytes it covered
  no longer exist. The reader will report the signature as invalid or damaged.
- **The sensible third path** is to remove the signature field and re-sign the
  document with your own certificate. The first is done by deleting `/Sig` from
  `/AcroForm`; the second requires tools with access to a private key (`pyHanko`,
  Acrobat). pdfedit does not apply signatures.

The `check` command reports the state of signatures: which fields exist, what signed
them, and whether the `/ByteRange` covers the whole file.

```
     digital signatures: 1
     field "Signature2" (/adbe.pkcs7.detached, signed D:20260804193722+07'00'):
     covers 4730785 of 4786468 bytes — file modified after signing
```

---

## Structural validation

The [`check`](#check-command) command,
and the `check_file` / `compare_files` functions from `pdfedit.validate`.
Validation works on local files only and sends nothing anywhere.

---

## Leaving no editing traces

This concerns the default mode — the full rebuild. The output file is created anew
and in its entirety: no layers appended at the end from which the original text
could be recovered trivially (if you want the opposite — the original left
inviolate at the cost of a visible edit layer — see
[Three saving modes](#three-saving-modes)). What is done so that the result differs
from the original in nothing but the changed text itself:

- **Metadata is not touched.** `/Info` and XMP are carried over as they are; only
  what the user set explicitly changes. The modification date is **not** updated
  automatically — there is a separate `--touch-moddate` option for that.
- **Libraries do not sign their work.** pikepdf writes itself into `pdf:Producer`
  and `xmp:CreatorTool` by default — that is suppressed.
- **`/ID` is preserved.** On writing, qpdf takes the first string of the identifier
  from the original, regenerates the second, and adds an `/ID` if there was none.
  The original value is restored by patching the already-written bytes; when the
  length matches, the file size does not change at all, and otherwise the result is
  verified by reopening the document.
- **Structure is reproduced.** The PDF version in the header, the presence of
  compressed `/ObjStm` object streams, and linearisation all follow the original. A
  PDF 1.4 that suddenly acquired object streams would give the edit away at once.
- **Content is not rewritten "canonically".** Only the changed operators are
  touched; unchanged streams and objects keep their original form.
- **A single `%%EOF`** — a dedicated check in the test suite.

The `verify` command compares the result against the original and shows exactly
what diverged; changes the user made deliberately are flagged separately and do not
count as discrepancies.

### Split text

The number "1234567" is one thing on screen, but the content stream may hold seven:
table and form generators place each digit with its own `Td` so the column aligns by
digit position. The parser honestly splits such text into separate runs — each with
its own state — because `BT`, `ET`, a change of font, size or character spacing all
close a run. Searching inside a run would never find such a number.

The search therefore works over **visual lines**: runs from the same stream sharing
a baseline with a small horizontal gap are joined into what the reader sees as
continuous (the gap rule is the same as the one used inside a run). A match is
stored in pieces, and the replacement lands like this: all the new text goes into
the first piece, and the rest are cleared.

Two consequences worth knowing:

- **The pieces are applied all together or not at all.** If the new text could not
  be placed into the first piece (not enough glyphs), the rest are left untouched
  too. Otherwise you would get the worst possible outcome: the old text already
  gone, the new text not yet there.
- **The line reflows from the first piece.** The other pieces lose their own
  coordinates: the text flows from the start of the first. If the pieces stood apart
  deliberately (table columns), the gaps will close up — and that is visible to the
  eye. Separate table cells never end up in one group: they are separated by a gap
  of more than two and a half em.

Runs from **different streams** are never joined: the appearance of a form field and
the page text may share a baseline, but they are different objects.

### Exact mode

The `--exact` flag enforces the rule "the page will contain exactly what was
specified and not one letter more". Everything that in normal mode helps the result
look tidy but changes what was asked for is switched off:

| what is disabled | why |
|---|---|
| width fitting (`Tz`, kerning) | it squeezes or stretches glyphs to fit the new text into the old space |
| line shift for justification | an extra `[n] TJ` instruction that nothing in the stream explains |
| the personal donor library | taking "whatever fits" from it is exactly the automatic substitution being avoided |
| system fonts | a system namesake has different outlines: substituting it for a donor glyph changes the document's appearance |

The glyph source is given with `--donor file.pdf` — and there are no others. What
the donor does not have is **not substituted with something similar**: the edit is
rejected, listing the missing characters.

```bash
python -m pdfedit replace contract.pdf -o out.pdf \
    --old "Total 100" --new "Итого 100" --exact --donor sample.pdf --inplace
```

Glyphs are copied from the donor **verbatim** — together with their point
coordinates and hinting instructions, rather than redrawn from the outline:
redrawing gives the same appearance but different bytes, and the glyph stops being
the donor's. A composite glyph (a letter made of a base and a diacritic) is
transferred along with its components; only the references to them change — which is
unavoidable, since glyph indices differ between two fonts.

Exact text matters more than exact position: if it does not fit into the original
stream, the edit is appended as a separate layer — with everything else preserved
(`/ID`, dates, `/Producer`, object numbering, compression level).

### Writing style: how an edit betrays itself even when it is correct

An edit can be substantively correct and still conspicuous. The same instruction can
be written a dozen equivalent ways, and every generator picks its own: Word writes
strings in hexadecimal (`<0048> Tj`), reportlab in parentheses (`(H) Tj`),
LibreOffice writes numbers at a fixed precision (`72.00`), fpdf however it comes out
(`72`). The parser does not care. But if strings are hexadecimal throughout the
document and suddenly parenthesised in one spot, the site of the edit is visible at a
glance in the decompressed stream — with no comparison to the original at all.

The style is therefore lifted **from the bytes of the very instruction** being
replaced (`pdfedit/style.py`), and the new one is written in the same style:

- hexadecimal strings stay hexadecimal, parenthesised stay parenthesised, and even
  the case of the hex digits is preserved;
- numeric precision is not changed: `700.00` does not become `700`, nor `700` become
  `700.00`;
- `Tj` does not turn into `TJ` or vice versa where the conversion loses nothing (a
  `TJ` array with kerning is never converted to `Tj` — those numbers carry meaning);
- even the separator between instructions and the presence of a space before the
  operator are preserved: `(text)Tj` and `(text) Tj` are different hands.

This applies in all three saving modes, not just in-place editing: the stream is
rewritten by targeted instruction substitution, and a full library reassembly remains
the fallback path for when targeted substitution is unsure about something.

### Compression: zlib level and stream tails

Two bits in the zlib header report how hard the data was compressed: level 6 gives
`78 9c`, levels 7–9 give `78 da`. Comparing those two bytes between the original and
the result is the cheapest way to see that a stream was recompressed by different
software, and the first thing anyone looks at. Streams are therefore recompressed at
the level they were compressed with.

A separate problem is **tails**. An in-place edit must fit the original length, and
the difference used to be padded with spaces past the end of the compressed data. The
decoder does not read them, but they remain in the file: unused bytes inside a stream
are evidence in themselves. Now the length is made up using the format's own means:
after `Z_SYNC_FLUSH` the stream is byte-aligned, and empty deflate stored blocks are
appended — five bytes each, carrying no data at all. The remaining one to four bytes
are made up by moving the tail of the data into the last block untouched. The
decompressed content is identical to the original byte for byte, and there is no tail
whatsoever.

Making the length come out exactly needs about ten bytes of slack. When there is
none, stronger compression provides it — but that changes the header. The decoder
does not read those two bits (RFC 1950 calls them advisory; `inflate` looks only at
the method and window size), so the header is restored to the original in full. If
even that does not help, padding remains — and `check --strict` reports it honestly.

### Hidden copies of the text

The same text sits in several places in the file at once, and editing the content
changes only one of them. `_sync_structure_text` brings them all into agreement:

| where | keys | what a mismatch costs |
|---|---|---|
| the structure tree of a tagged PDF | `/ActualText`, `/Alt`, `/E` | PyMuPDF and screen readers prefer the copy over the page content and show the old text |
| markup inside the stream itself | `/Span <</ActualText …>> BDC` | the copy is invisible when walking objects — it is an instruction operand |
| form fields | `/V`, `/DV`, `/TU`, `/RV` | the field value disagrees with what is drawn in `/AP` |
| annotation appearance | the `/AP /N` stream | the old value stays on screen despite a new `/V` |
| annotations | `/Contents`, `/RC`, `/Subj` | the original wording survives in the note |
| bookmarks | `/Title` | the old text is visible in the outline |

The field name (`/T`) is deliberately left alone: software locates the form by it.

### Fonts

Copying glyphs from a donor (`fontops.copy_glyphs_from_donor`) is done so that the
result differs from the original program by exactly the added glyphs:

- `recalcTimestamp=False` — otherwise fontTools writes the current time into
  `head.modified`, and a font created in 2002 turns out to have been "modified"
  today;
- `recalcBBoxes=False` — otherwise the bounding boxes of **all** glyphs are
  recomputed, including untouched ones; the box is computed for added glyphs only
  (without it the glyph would not render at all);
- the physical order of tables is restored: `TTFont.save` lays them out in its own
  canonical order — on Arial that moves `cmap` from twenty-second place to eighth;
- the hinting tables (`fpgm`, `prep`, `cvt`) stay byte-identical;
- the width of a new glyph reaches `hmtx` and `/W` from a single source
  (`fontops.width_to_pdf`), so the values cannot diverge;
- **no orphan glyphs are left behind.** Glyphs are obtained in advance — before the
  edit is applied — because without them there is no telling whether the edit is
  feasible at all. If the edit ultimately does not happen, the font is rolled back to
  a snapshot in full (`fontops.FontSnapshot`): a glyph no code refers to draws
  nothing, but announces plainly that the font was edited, and even which letters
  were needed for it.

**What must honestly be said about the limits.** Byte-level indistinguishability is
unattainable and is not promised: qpdf rewrites the file in full, so object order,
indentation and the compression parameters of unchanged parts may differ from the
original. If glyphs were added to a font, its program has certainly changed. The
claim is that the document carries no **traces of editing** — no tool markers, no
mismatched metadata, no appended layers — not that the fact of a re-save could not be
established by a byte-level comparison against a known-good original.

---

## Programmatic interface

```python
from pdfedit import PdfEditor, verify

# a simple replacement
with PdfEditor("contract.pdf") as editor:
    report = editor.replace("Smith", "Jones")
    print(f"replaced: {len(report.applied)}")
    editor.save("contract-fixed.pdf")

print(verify("contract.pdf", "contract-fixed.pdf").describe())
```

Incremental saving with verification of the result:

```python
from pdfedit import PdfEditor, check_file, compare_files

with PdfEditor("contract.pdf") as editor:
    editor.replace("Smith", "Jones")
    editor.save("contract-fixed.pdf", incremental=True)
    print(editor.last_incremental_report.describe())

# is the file intact on its own?
structure = check_file("contract-fixed.pdf")
print(structure.describe())
assert structure.valid

# what changed compared with the original?
diff = compare_files("contract.pdf", "contract-fixed.pdf")
print(diff.describe())
assert diff.original_bytes_kept         # original bytes still in place
assert diff.object_numbers_kept         # references lead to the same objects
assert not diff.unexpected_differences  # nothing beyond the edit itself
```

Searching and selective editing:

```python
with PdfEditor("contract.pdf") as editor:
    editor.parse()

    for match in editor.find("2021", pages=[0]):
        print(f"p.{match.page_index + 1}: {match.run.text!r} at {match.run.bbox}")

    # replace only the occurrence in the document header
    first = editor.find("2021")[0]
    editor.apply_edits([first.to_edit("2022")])
    editor.save("out.pdf")
```

Editing metadata:

```python
from pdfedit import PdfEditor
from pdfedit.metadata import apply_metadata

with PdfEditor("in.pdf") as editor:
    apply_metadata(editor.pdf, {
        "/Author": "J. Jones",
        "/CreationDate": "2019-01-01 10:00:00",
        "/Keywords": None,          # None removes the field
    })
    editor.save("out.pdf")
```

Useful objects:

| Object | Purpose |
|---|---|
| `PdfEditor` | loading, parsing, searching, replacing, saving |
| `PdfEditor.runs` | a list of `TextRun` — runs with text, font and `bbox` |
| `Match` | a found occurrence; `.to_edit(new_text)` produces an edit instruction |
| `EditSpec` | an edit instruction, serialisable to JSON |
| `ApplyReport` | what was applied, what was skipped and why, what happened to the fonts |
| `verify()` | compares two files by metadata and structure |

---

## Tests

```bash
python -m unittest discover -s tests -v
```

225 checks: CMap parsing and assembly, byte-level accuracy of re-encoding, geometry
agreement with an independent parser, all fitting modes, font extension, text in
XObjects, the fallback font, preservation of metadata and `/ID`, all three saving
modes, the CLI commands.

One check deserves a special mention: `test_glyphs_are_actually_drawn` counts the ink
on the rendered page. Text that can be **extracted** but is not **visible** is the
most treacherous failure when editing font subsets, and comparing strings will never
catch it.

`tests/test_integrity.py` verifies the absence of editing traces on documents written
in **different hands** (`tests/generators.py`). reportlab and fpdf2 are invoked for
real; Word and LibreOffice cannot be installed on this machine, so their hands are
reproduced manually — from the traits those packages leave in a file (hexadecimal
strings and `/ActualText` for Word; plain `/TrueType` with `/Differences` and
compression level 9 for LibreOffice). The functions are named accordingly:
`word_style`, `libreoffice_style`. The variety is not decoration — nearly every bug in
style preservation shows up in only one of the hands: what carefully edits a reportlab
file will happily break a file from Word.

The replacements in these checks are **digit for digit**. Width is no trifle: if the
new text is wider or narrower than the old, the editor adjusts the line by horizontal
scaling, two `Tz` instructions appear in the stream, the edit stops fitting the
original length and goes into the appended layer. Digits are the same width in almost
every font, so digit-for-digit replacement moves nothing. It is also the most common
edit in real life — a typo in a number or an amount.

---

## Limitations

- **Replacement within a single line.** Text broken across several showing
  operators is found and replaced as a whole (see [Split text](#split-text)), but a
  line break remains a boundary: the program reports this and offers to replace the
  parts separately.
- **Type3 fonts** (whose glyphs are themselves drawing programs) are read-only.
- **Font extension works for TrueType.** For CFF/Type1 a fallback font is embedded
  instead.
- **Scanned documents have nothing to edit:** there is no text in them. The program
  reports that no runs were found.
- **Password protection is not fully reproduced on a full rebuild**: the owner
  password and `/ID` cannot be restored (see
  [Encrypted documents](#encrypted-documents)). With incremental saving, protection
  is preserved completely.
- **A digital signature is invalid after an edit** — that is a property of
  signatures, not a limitation of the program (see
  [Digital signatures](#digital-signatures)).
- **Shared Form XObjects.** If one XObject is used by several pages, the edit shows
  on all of them — a property of the document itself.
- **Composite fonts with an explicit `/CIDToGIDMap`** are not extended; a fallback
  font is used for them.
- **An in-place edit does not always fit.** Text of identical length may compress
  differently: across 60 real documents, 40 fitted entirely in place and 14 partly
  went into the appended layer. There is also the fundamentally unsolvable case —
  when the best possible compression yields 113 bytes against a capacity of 112. The
  edit then honestly goes into a layer, and `check --strict` shows what gives it away.
- **Objects inside an `/ObjStm` are not edited in place** — this limitation is
  fundamental: objects are packed together inside a compressed object stream, and
  editing one would disturb its neighbours. Structure-tree nodes with `/ActualText`
  most often end up there, which is why tagged documents go into a layer more often
  than others. Ordinary dictionary objects (structure-tree nodes, bookmarks, form
  fields) are edited in place on equal terms with streams, provided their
  representation has not grown longer.
- **An exact length needs slack.** When the data compresses to nearly its original
  size, there is not enough room for the closing stored block, and a padding tail
  remains behind it: on those same 60 documents — 11 out of 39. The `stream-tail`
  check reports it.
- **Only two filter chains are reassembled** — `/FlateDecode`, and `/ASCII85Decode`
  on top of it (which is how reportlab writes). A stream with any other filter, or
  with `/DecodeParms`, is not edited in place and goes into the appended layer.
- **Style is reproduced for content streams.** The manner of writing dictionaries and
  the trailer is left to qpdf on a full rebuild.

---

## Source layout

| File | Contents |
|---|---|
| `pdfedit/cmap.py` | parsing and assembling CMaps: `/ToUnicode` and composite font encodings |
| `pdfedit/encodings_tables.py` | standard single-byte encodings for simple fonts |
| `pdfedit/fonts.py` | the font model: encoding, decoding, metrics, glyph presence, system font lookup |
| `pdfedit/fontops.py` | adding glyphs to embedded fonts, embedding fallbacks |
| `pdfedit/content.py` | content stream parsing, state tracking, text runs |
| `pdfedit/editor.py` | searching, planning edits, reassembling text-showing operators |
| `pdfedit/metadata.py` | reading and editing `/Info`, XMP synchronisation, date formats |
| `pdfedit/saving.py` | saving without editing traces, and verifying the result |
| `pdfedit/incremental.py` | appending an edit layer: new object revisions, `xref` table or `/XRef` stream, self-verification |
| `pdfedit/inplace.py` | editing streams over the old bytes without changing file length |
| `pdfedit/streampatch.py` | instruction boundaries in a content stream and targeted substitution of changed ones |
| `pdfedit/style.py` | the manner of writing operands: hex strings or parentheses, numeric precision, `Tj` versus `TJ` — lifted from the original and reproduced |
| `pdfedit/traces.py` | hunting for editing traces: stream tails, compression level, orphan glyphs, width mismatches, hidden text copies |
| `pdfedit/pdfcrypt.py` | encryption of appended objects: AES and RC4, object keys |
| `pdfedit/validate.py` | structural integrity checks and object-tree diffing of two documents |
| `pdfedit/cli.py` | the command line |
| `pdfedit/gui.py` | the Tkinter graphical editor |
| `app/build_app.py` | building the pdfedit.app bundle for macOS |
| `app/make_icon.py` | the application icon (drawn programmatically) |
| `samples/make_samples.py` | generator of test documents, including `sample_layered.pdf` — the hardest profile: transparency, masks, three kinds of font, a link |
| `tests/test_pdfedit.py` | core checks |
| `tests/test_incremental.py` | incremental saving, encryption, and the AES test vectors |
| `tests/test_validate.py` | the validator: corrupted documents must be recognised as such |
| `tests/test_inplace.py` | in-place editing and the instruction parser |
| `tests/generators.py` | documents in four hands: reportlab and fpdf2 for real, Word and LibreOffice by reproducing their traits |
| `tests/test_integrity.py` | absence of editing traces: writing style, compression level, tails, hidden copies, fonts |
| `tests/test_split_and_exact.py` | split text, form fields (`/V` together with `/AP`), exact mode, and the verbatim copying of donor glyphs |

---

## Intended use

This program is intended solely for lawful use: correcting typos in official
documents without losing their original properties, working with archives where
metadata must remain unchanged, and testing PDF-processing systems. Responsibility
for the lawfulness of editing any particular document rests with the person
performing the edit.

---

## License

MIT — see [LICENSE](LICENSE).
