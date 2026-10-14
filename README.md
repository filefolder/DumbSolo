# DumbSolo v0.1 (prototype)

Single-file, cross-platform harvester for DigiSolo / IGU-16HR seismic nodes. 
This is a much simpler version focusing on passive seismic / FDSN rather than engineering applications. 

To run,
    pip install -r PySide6>=6.5 obspy
    python dumbsolo.py
... or use the compiled executable!

S/O https://github.com/TobbeTripitaka/node_toolbox for assistance decoding DLD

## Projects
Project ▸ New… creates a project folder; Project ▸ Open… / Open recent switch
between them. Each project folder holds `dumbsolo_project.json` and, by default:

    <project>/sds/                 SDS archive + harvest DB (dumbsolo_harvest.sqlite)
    <project>/inventory.xml        StationXML
    <project>/raw_data/            raw node backups (.tar.gz)
    <project>/raw_data/extracted/  archives loaded back for processing

Paths can be changed in the GUI; paths inside the project are stored relative,
so a project folder can be moved. Network/location codes, the gap threshold and per-deployment edits
(station code, coordinates, rotation) are saved in the project and re-applied
whenever that node is loaded. The last project reopens on start-up.

## Deployments (splitting at gaps)
When a node is read, every record header of one component is scanned for data
gaps (in a background thread). Each node is split into separate deployments at
gaps longer than "Split at gaps >" (default 1 h, saved per project). Each
deployment gets its own table row (`serial (1/2)` …), start/end, and position
(outlier-cleaned mean of the GPS fixes logged during that deployment; see Positions), and becomes its own
station epoch in StationXML. The station code defaults to the last 5 digits of
the serial; codes used by more than one row are highlighted red. Harvesting
refuses duplicates that overlap in time and asks for confirmation otherwise.
The details pane lists the node's gaps.

## Importing station names
"Import station names…" reads a CSV with `serial, station, start, end` (UTC;
header optional; comma/semicolon/tab; blank start or end = open-ended; a date
without a time means the start/end of that day). For each node:

* boundaries between its consecutive entries are moved to the nearest real data
  gap (within `SCHEDULE_SNAP_HOURS`, default 12 h, source-code setting) and the
  node is split there, even if that gap is shorter than the "Split at gaps"
  threshold; with no gap nearby the boundary is reported and not split;
* each deployment takes the station name of the entry overlapping it most.

The import is saved in the project (re-importing a node's serial replaces its
entries) and replaces earlier manual station edits for those nodes; later
manual edits win. The log reports moved boundaries and entries that couldn't be
applied. Project ▸ Clear imported station names undoes it.

## Map
"Map" plots station positions from the harvest DB (filled) plus loaded
deployments not harvested yet (hollow), labelled with station codes.

## Raw backups
Tick "Archive raw node data (.tar.gz)" (Processing panel, saved per project) and
"Harvest checked nodes" first backs up each checked USB node volume (everything on it, minus OS
litter such as `System Volume Information`) to
`raw_data/<serial>_<first day>-<last day>.tar.gz`, with a SHA-256 manifest.
Existing archives are skipped. Only mounted USB volumes are archived (not nodes
loaded from folders or archives); a node that is no longer mounted is never
archived (no empty archives) and is removed from the table. Format: gzip level 1 — MiniSEED is
already Steim-compressed, so gzip only saves ~15–20 % on it, and level 1 gets
most of that at a fraction of the CPU of higher levels (set
`ARCHIVE_COMPRESSLEVEL`). Standard tar.gz, so `tar xzf` works anywhere.

"Load archive…" extracts archives into `raw_data/extracted/` (verifying every
file against the manifest; reused if already extracted) and adds the nodes to
the table, marked "archive". They can be harvested like mounted nodes, but are
never wiped.

## Data formats
Nodes may hold MiniSEED (`seisNNN?.MiniSeed`) and/or the vendor DLD format
(`seisNNN?.DLD`); both are read automatically, and a node can mix them. If the
same file number and component exists in both, `PREFER_FORMAT` decides
(default MiniSEED; noted in the details pane). DLD files are decoded from their
raw layout (per node_toolbox `docs/DLD_FORMAT.md` / `lib/smartsolo_dld.py`):
512-byte header, then blocks of int24 samples (1000 so far; the block size is
detected from the first time tag) each followed by a 72-byte GPS time tag.
Blocks count as continuous only if both tag time and tag tick advance by one
block. Sample rate comes from the tag spacing. Leap seconds follow the
historical GPS−UTC table for pre-2017 data. The details pane shows the longest
time a DLD recording went without a GPS sync (a timing-quality indicator).
Time = GPS week + time-of-week − `GPS_UTC_LEAP_S` (18).
DLD samples include the preamp gain and raw polarity, so they get the same gain removal and polarity
flip as MiniSEED. If there is no usable `DigiSolo.LOG`, positions come from the
DLD time tags, and missing serial/firmware/script from the DLD header.

## Processing
* Node MiniSEED is decoded, divided by 10^(gain_dB/20), multiplied by −1,
  optionally rotated, rounded to int32 and written as Steim2 (4096-byte records).
* Channels `??Z`, `??N` (X), `??E` (Y); band and instrument code by node type and sample rate (e.g. `DPZ`). Sample rate from the MiniSEED header,
  cross-checked against `sct_par.xml` (`Sample_Rate` in 10 µs units: 400 → 250 Hz).
* Exact UTC day files; re-harvested data replaces earlier data for the same span.
* Optional time range (From inclusive, To exclusive); harvest tracking is
  window-aware.
* Parallel: one job per node component in a process pool.

### Source-code-only settings ("DATA TRANSFORM" block in `dumbsolo.py`)
* `REMOVE_PREAMP_GAIN`, `FLIP_POLARITY` – after changing either, harvest into a
  **new, empty SDS root** and use a matching response.
* `MSEED_RECORD_LENGTH` – default 4096.
* `WIPE_KEEP` – files kept by "wipe" (default: only `device.ini`).
* `PREFER_FORMAT`, `GPS_UTC_LEAP_S`, `DLD_TAG_MARKS`, `DLD_TOW_OFFSET_S` – DLD reading.
* `ARCHIVE_COMPRESSLEVEL`, `ARCHIVE_SKIP` – raw backups.

## Rotation
"Apply rotation (°)" column (default 0; sensors assumed oriented to north), per deployment.
Non-zero rotates the +N/+E data: N′ = N cos θ − E sin θ, E′ = N sin θ + E cos θ
(clockwise positive; θ = +90 puts northward motion on +E).

## Responses
`SENSOR_TYPES` in `dumbsolo.py` maps each `device_type` (as in `sct_par.xml`) to
an FDSN instrument code, the sensor's corner period and a response builder:

* `IGU-16 1C 5Hz`, `IGU-16HR 3C 5Hz`: `P` (geophone), corner 0.2 s (5 Hz) ->
  `DP?` at 250-500 Hz, `GP?` at 1000 Hz
* `IGU-16 BD3C 5s`: `H` (high-gain seismometer), corner 5 s -> `DH?` / `GH?`

The band code follows the full FDSN table from the sample rate and the corner
period: corners < 10 s use the short-period bands (G, D, E, S), >= 10 s the
broadband bands (F, C, H, B), so a future broadband node only needs its corner
period set (e.g. 120 s -> `CH?` at 250 Hz, `HH?` at 100 Hz). Unknown types get
instrument code `P`, short-period bands, a warning and no response.

## Fonts / text
All text DumbSolo shows (and the source file itself) is plain ASCII, so no
symbol can be missing from a system font (e.g. on older macOS). Keep it that
way when editing: write "deg", "->", "..." rather than special characters.

## Unplugged nodes and unmounting
Every few seconds DumbSolo checks that loaded nodes are still there; unplugged
volumes (and vanished folders) are removed from the table, and come back when
plugged in again (Scan drives / Auto-detect). "Unmount selected" (highlighted
rows) and "Unmount all" safely unmount USB node volumes (Linux: `udisksctl`,
falling back to `gio mount -u` / `umount`; macOS: `diskutil unmount`; Windows:
Explorer's Eject). Nodes loaded from folders or archives are left alone.

## Wipe
Deletes everything on each checked node (all files and sub-folders) except
`device.ini` (`WIPE_KEEP`) and folders the operating system manages on the volume.
Nodes not fully harvested to the current SDS root trigger a second warning
(wipe harvested only / wipe all anyway / cancel). The dialogs also show whether
each node has a raw backup.

## Positions
Each deployment's latitude, longitude and altitude are the **mean of its GPS
fixes after removing outliers**:

1. Only fixes logged while the node was recording are used (fixes from transport
   before planting or after pickup are ignored), unless fewer than
   `POSITION_MIN_FIXES` (10) fall inside, e.g. a very short test recording; then
   all fixes are used.
2. A fix is rejected if it is more than `POSITION_OUTLIER_SIGMA` (3) robust
   standard deviations (1.4826 x MAD) from the median, north and east separately
   and in altitude, with minimum tolerances of `POSITION_MIN_TOL_M` (5 m) and
   `ALTITUDE_MIN_TOL_M` (10 m). This repeats until nothing more is rejected.
3. The remaining fixes are averaged.

The Fixes column shows how many fixes were used; the details pane gives the
numbers rejected and ignored, and the scatter of the fixes used. Fixes come from
`DigiSolo.LOG`, or from the DLD time tags if there is no log.
