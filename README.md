# DumbSolo

DumbSolo is a single-file, cross-platform (Win,OSX,Linux) desktop GUI utility for collecting data
from DigiS0lo-style seismic nodes (IGU-16HR 3C, IGU-16 1C and BD3C-5 types).
Mount the nodes and DumbSolo will:

- read their data (MiniSEED and/or the DLD format) and settings
- write the data to an SDS archive, with gain removed and polarity corrected (FDSN standard)
- apply station names given a user-defined CSV (or can be done manually)
- optionally rotate misaligned azimuths
- write an FDSN StationXML inventory with instrument responses
- keep track of what has already been harvested
- back up whole nodes to `.tar.gz` and to optionally load those backups again later
- wipe nodes and program them with a new user-supplied settings file.

What it WON'T and will never do:

- update/program firmware
- create new settings files (will have to manually edit pre-existing)
- any of the other more engineering-based stuff Sol0Lite does

Most of this code and README is originally AI (sorry!!) but we have tested it fairly extensively in our lab.
It should be working more or less as expected, but bug reports and PRs gladly received.

![image](https://github.com/filefolder/DumbSolo/blob/main/dumbsolo_screenshot.png)

---

## Installation

You need Python 3.10 or newer.

```bash
pip install PySide6, obspy, matplotlib
python dumbsolo.py
```

It's probably easy enough to not require a VENV, but if you are so inclined:
```bash
python3 -m venv ~/dumbsolo-env
~/dumbsolo-env/bin/pip install PySide6 obspy matplotlib
~/dumbsolo-env/bin/python dumbsolo.py
```

For a Windows `.exe`, see [Building a Windows executable](#building-a-windows-executable).

---

## Quick start

1. **Create a project.** Use *Project > New...* and choose a folder. Everything
   for one experiment lives there.
2. **Set the network code** (and location code, if any) in the *Naming* panel.
3. **Plug in the nodes** and press *Scan drives*, or tick *Auto-detect new drives*.
   Each node appears as a row in the table. Nodes copied to disk can be added
   with *Add folder...*, and backups with *Load archive...*.
4. **Check the table.** Look at station codes (duplicates are shown in red),
   positions, start and end times, and the *Status* column for warnings.
5. **Press *Harvest checked nodes*.** Ticked nodes are written to the SDS archive
   and the StationXML inventory is updated.
6. Optionally **wipe** the nodes, **program** them for the next deployment, and
   **unmount** them.

---

## Projects

*Project > New...* creates a project folder, *Project > Open...* or *Open recent*
switches between projects, and the last project reopens on start-up. A project
folder contains `dumbsolo_project.json` and, by default:

```
<project>/sds/                 SDS archive and harvest database (dumbsolo_harvest.sqlite)
<project>/inventory.xml        StationXML inventory
<project>/raw_data/            node backups (.tar.gz)
<project>/raw_data/extracted/  backups unpacked by "Load archive"
<project>/sct_par_backups/     previous settings files saved when programming
```

All paths can be changed in the *Output* panel. If the StationXML field is left
blank, `inventory.xml` is written next to the SDS folder (in the folder that
contains it). Paths inside the project folder are stored relative to it, so a
project can be moved or copied.

The project also remembers the network and location codes, the gap threshold,
the archive setting, imported station names, and any edits you make in the
table (station codes, coordinates, rotation). These are reapplied whenever the
same node is loaded again.

---

## The node table

Each row is one **deployment** of one node (see [Deployments](#deployments)).

| Column | Meaning |
|---|---|
| Serial | Node serial number. The checkbox selects the node for harvesting, archiving, wiping and programming. |
| Station | Station code (editable). Defaults to the last 5 digits of the serial. Red if used by more than one row. |
| Harvest | *new*, *partial* or *harvested*, relative to the current SDS folder and time range. |
| Status | *ready*, a warning, *NO DATA*, or the result of the last action. |
| Size (MB) | Amount of data in this deployment. |
| Script, Firmware | From the node's settings file and log. |
| Start, End (UTC) | Time span of the data. |
| Latitude, Longitude, Elev | Position (editable; see [Positions](#positions)). |
| Fixes | Number of GPS fixes used for the position. |
| SR, Gain | Sample rate (Hz) and preamp gain (dB). |
| Apply rotation (deg) | Optional rotation of the horizontal components (editable; see [Rotation](#rotation)). |
| Path | Where the node is mounted or stored. |

Columns can be dragged into a different order (right-click the header to reset).
Selecting a row shows full details in the *Node details* pane: files, data gaps,
GPS statistics, the geophone self-test, and the node's settings.

A node with no data files shows **NO DATA**. If it shows **NO DATA (?)**, it has
data files whose names DumbSolo doesn't recognise; hover over the status to see
them.

---

## Harvesting

*Harvest checked nodes* processes every ticked row:

- **Data** is decoded, divided by the preamp gain (10^(gain/20)), multiplied by
  -1 to give the standard polarity convention, optionally rotated, rounded to
  32-bit integers and written as Steim2 MiniSEED (4096-byte records) into
  daily SDS files, split exactly at UTC midnight.
- **Channel codes** follow the FDSN convention, e.g. `DPZ`, `DPN`, `DPE` for a
  250 Hz geophone. Node component X becomes N, Y becomes E, and Z stays Z.
  Single-channel nodes are treated as vertical.
- **StationXML** is written when *Write StationXML* is ticked. It covers either
  every deployment in the harvest database or only the ticked nodes.
- **Parallel workers** sets how many components are processed at once.
- **Time range (UTC)** limits harvesting to a window (From inclusive, To
  exclusive). It defaults to the span of all loaded data; *All loaded data*
  resets it.
- **Skip already-harvested data** avoids reprocessing. DumbSolo records each
  harvested file in `dumbsolo_harvest.sqlite` in the SDS folder, including the
  time window and rotation used, so a later harvest only processes what is new
  or changed.
- **Archive raw node data** first backs up each ticked USB node (see
  [Backups](#backups)).

Harvesting again over data that is already in the archive replaces it for that
time span; it never creates duplicates.

### Deployments

When a node is loaded, DumbSolo scans its data for gaps. A gap longer than
*Split at gaps >* (default 1 hour) splits the node into separate deployments,
shown as `serial (1/2)`, `serial (2/2)` and so on. Each deployment has its own
station code, position and rotation, and becomes its own station epoch in
StationXML.

Using the same station code for two deployments is allowed if they don't
overlap in time (DumbSolo asks for confirmation). Overlapping duplicates are
refused.

### Importing station names

*Import station names...* reads a CSV file of station names for nodes:

```
serial,station,start,end
453021942,ST001
453000001,ST002,,2026-09-16 00:05
453000001,ST003,2026-09-16 00:06,
453000002,ST004,2026-09-15,2026-09-20
```

- Only serial and station are required. A blank start or end means "from the
  beginning" or "to the end", and a date without a time covers the whole day.
  Times are UTC. A header line is optional, and commas, semicolons or tabs can
  separate the columns.
- Each deployment gets the name of the row that overlaps it most.
- Where one row for a node ends and the next begins, the node is split at the
  nearest real data gap within 12 hours. With no gap nearby, the boundary is
  reported in the log and the node is not split.

The import is saved in the project. Edits made in the table afterwards take
priority. *Project > Clear imported station names* removes it.

### Positions

Each deployment's latitude, longitude and elevation are the **average of its
GPS fixes after removing outliers**:

1. Only fixes logged while the node was recording are used, so fixes from
   transport before planting or after pickup are ignored. If fewer than 10 fixes
   fall inside the recording, all fixes are used.
2. Fixes more than 3 robust standard deviations from the median are rejected,
   checking north, east and altitude separately. Fixes within 5 m horizontally
   or 10 m vertically are always kept. This repeats until nothing more is
   rejected.
3. The remaining fixes are averaged.

The details pane shows how many fixes were used and rejected and their scatter.
Fixes come from the node's `DigiSolo.LOG`, or from the DLD time tags if there is
no log. Coordinates can also be typed into the table.

### Rotation

Sensors are assumed to be oriented to north. *Apply rotation (deg)* (default 0)
rotates a deployment's horizontal data:

```
N' = N cos(a) - E sin(a)
E' = N sin(a) + E cos(a)
```

Positive angles are clockwise, so a = +90 moves northward motion onto the east
component. The rotation is recorded in the harvest database; changing it marks
the deployment for re-harvesting.

---

## Data formats

Nodes can record MiniSEED (`seis000X.MiniSeed`) or the DLD format
(`seis000X.DLD`). Both are read automatically, and a node can contain both. If
the same file exists in both formats, MiniSEED is used. Single-channel nodes
write files without a component letter (`seis000.DLD`).

DLD files store the samples together with GPS time tags. DumbSolo derives the
time of each sample from the GPS week and time of week in these tags. This
timing has been checked against independently timed co-located sensors. The
details pane shows the longest time a DLD recording ran without a GPS sync, as
an indicator of timing quality.

### What DumbSolo reads from a node

Only these files, from the node's top folder:

| File | Used for |
|---|---|
| `device.ini` | Serial number, firmware. Required: a folder without it is not treated as a node. |
| `sct_par.xml` (or `SCT_INT.XML`) | Device type, channel gains, sample rate, channel count, script name. |
| `DigiSolo.LOG` | GPS fixes, compass and tilt, geophone self-test. |
| `seisNNN[X/Y/Z].MiniSeed`, `seisNNN[X/Y/Z].DLD` | Waveform data. |

Archiving copies everything on the node. Wiping and programming are described
below.

---

## Instrument responses

Responses are built into the program for these device types:

| Device type (as in `sct_par.xml`) | Sensor | Channels at 250 Hz |
|---|---|---|
| `IGU-16HR 3C 5Hz` | 5 Hz geophone, 76.6 V/(m/s) | DPZ, DPN, DPE |
| `IGU-16 1C 5Hz` | 5 Hz geophone, 76.6 V/(m/s) | DPZ |
| `IGU-16 BD3C 5s` | 5 s short-period seismometer, 209.4 V/(m/s) | DHZ, DHN, DHE |

Some units report shortened names (`IGU-3C 5Hz`, `IGU-1C 5Hz`); these are
accepted too. Matching ignores case, spaces, hyphens and underscores.

The responses describe the data as DumbSolo writes it (gain removed, polarity
corrected). Band codes follow the FDSN table from the sample rate and the
sensor's corner period, e.g. `GPZ` at 1000 Hz. A node of an unknown type is
still harvested, but gets a warning in the table and no response in StationXML.
See [Adding a device type](#adding-a-device-type).

---

## Backups

Tick **Archive raw node data (.tar.gz)** before harvesting. Each ticked USB node
is first copied in full to `raw_data/<serial>_<first day>-<last day>.tar.gz`,
with a checksum for every file. Nodes that already have an archive are skipped.
Only mounted USB nodes are archived, not nodes loaded from folders or archives.

**Load archive...** unpacks archives into `raw_data/extracted/`, checks every
file against its checksum, and adds the nodes to the table marked *archive*.
They can be harvested like plugged-in nodes, but are never wiped or programmed.
The archives are ordinary `.tar.gz` files, so any archive tool can open them.

---

## Wiping nodes

**Wipe checked nodes...** deletes everything on each ticked node except
`device.ini` and `sct_par.xml`, so the node keeps its identity and settings.
Folders managed by the operating system are left alone.

Two safeguards apply. The confirmation shows how many files will be deleted and
whether each node has a backup. If a node has not been fully harvested to the
current SDS folder, a second warning offers to wipe only the harvested nodes;
*Cancel* is the default.

---

## Programming nodes

**Program checked nodes...** copies a settings file of your choice onto each
ticked USB node as `sct_par.xml`. Before anything is written, the file is
checked:

- It must be well-formed XML with a `<script_Information>` root, no duplicate
  tags, and plain text only.
- `script_file_name`, `device_type`, `ChNumber`, `Sample_Rate`, `begin_Day`
  (YYYY/MM/DD), `begin_time` and `end_time` (HH,MM,SS) must be present and
  valid, with a type and gain (0-36 dB) for every channel.
- `ChNumber` must match the device type (a "3C" type has 3 channels).
- **The file's `device_type` must exactly match the node's own device type**,
  read from the settings file or log already on the node. Nodes of another type
  are refused. Nodes with no settings file or log can't be checked; program
  those with SoloLite or copy the file onto them yourself.

The confirmation shows the file's settings (script, device type, output format
(MiniSEED or DLD), channels and gains, sample rate, start time) and lists which
nodes will and won't be programmed. Each node's previous `sct_par.xml` is saved
to `<project>/sct_par_backups/`. The new file is read back after writing to
check it.

---

## Unplugging and unmounting

DumbSolo checks every few seconds that loaded nodes are still present. Nodes
that have been unplugged disappear from the table, and reappear with their
settings when plugged in again.

**Unmount selected** (highlighted rows) and **Unmount all** safely eject USB
nodes, using `udisksctl` (or `gio` / `umount`) on Linux, `diskutil` on macOS, and
Eject on Windows. Nodes loaded from folders or archives are not affected.

---

## Map

**Map** plots station positions: harvested deployments as filled triangles,
loaded but unharvested ones as hollow triangles, each labelled with its station
code. The plot can be zoomed and saved.

---

## Advanced settings

Some settings change how data is processed and are deliberately not available in
the program window. They are near the top of `dumbsolo.py` (the "DATA
TRANSFORM" block):

| Setting | Default | Purpose |
|---|---|---|
| `REMOVE_PREAMP_GAIN` | True | Divide data by the preamp gain. |
| `FLIP_POLARITY` | True | Multiply data by -1. |
| `MSEED_RECORD_LENGTH` | 4096 | Output MiniSEED record size in bytes. |
| `WIPE_KEEP` | device.ini, sct_par.xml | Files kept when wiping. |
| `PREFER_FORMAT` | mseed | Format used when both MiniSEED and DLD exist. |
| `SCHEDULE_SNAP_HOURS` | 12 | How far an imported station-name boundary may move to reach a data gap. |
| `POSITION_*`, `ALTITUDE_MIN_TOL_M` | see code | Position outlier rejection. |
| `ARCHIVE_COMPRESSLEVEL` | 1 | gzip level for backups. |

If you change `REMOVE_PREAMP_GAIN` or `FLIP_POLARITY`, harvest into a **new,
empty SDS folder** (so data processed in different ways isn't mixed), and make
sure the instrument responses match.

### Adding a device type

Add an entry to `SENSOR_TYPES` in `dumbsolo.py`, giving the FDSN instrument code,
the sensor's corner period in seconds, and a function that builds its response
(copy one of the existing `_resp_...` functions). The corner period selects the
band codes: under 10 s gives the short-period set (G, D, E, S), 10 s or more the
broadband set (F, C, H, B).

---

## Building a Windows executable

On a Windows machine (PyInstaller can't build for Windows from another system):

```bat
py -3.12 -m venv build-env
build-env\Scripts\activate
pip install PySide6 obspy matplotlib pyinstaller
pyinstaller dumbsolo.py --name DumbSolo --windowed --onedir --noconfirm ^
    --collect-all obspy --copy-metadata obspy ^
    --hidden-import matplotlib.backends.backend_qtagg ^
    --exclude-module tkinter
```

The program is in `dist\DumbSolo\`. Zip that folder to distribute it; users
unzip it and run `DumbSolo.exe`. Rebuild after every change to `dumbsolo.py`.

---

## Troubleshooting

**Most problems are fixed by restarting the code and/or opening a new project!**
Try this first.

**A node shows NO DATA (?).** It has data files whose names aren't recognised;
hover over the status to see them.

**No response in StationXML.** The node's device type isn't in `SENSOR_TYPES`;
the *Status* column names it. See [Adding a device type](#adding-a-device-type).

**Odd characters in the window.** All text DumbSolo displays is plain ASCII (by design)
but across so many computers and fonts issues still appear. Let us know. 
