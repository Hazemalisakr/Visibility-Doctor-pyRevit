# Visibility Doctor — pyRevit Extension for Revit 2024

## What It Does

A **read-only diagnostic tool**: given a document, an Element ID, and a host view,
Visibility Doctor explains *why* that element is not visible in the view.

It checks: element existence, explicit hide, category visibility, view-template
control, workset visibility, view filters, phasing, and design options.

No model changes are made — ever.

## Installation

1. Copy the entire `VisibilityDoctor.extension` folder to a location on disk
   (e.g. `C:\pyRevitExtensions\VisibilityDoctor.extension`).

2. In Revit → pyRevit tab → Settings → Custom Extension Directories,
   add the **parent** folder (e.g. `C:\pyRevitExtensions`).

3. Reload pyRevit (pyRevit tab → Reload).

4. A new tab **VisibilityDoctor** should appear in the Revit ribbon,
   with a **Diagnostics** panel containing the **Visibility Doctor** button.

## Icon

Replace `icon.png` inside the pushbutton folder with a 96 × 96 PNG to
display a custom icon on the ribbon button.

## Requirements

| Requirement      | Value                                  |
|------------------|----------------------------------------|
| Revit            | 2024                                   |
| pyRevit          | 4.8+ with default IronPython 2.7 engine|
| Admin rights     | Not required                           |
| External packages| None                                   |

## Limitations (V1)

- **Link diagnosis** is a stub — selecting "Linked Model" will inform you
  that link diagnosis is not yet implemented.
- Geometry checks (view range, crop region, section box) are **not in scope**
  for V1 but the engine is structured to accept a `diagnose_geometry()` step.
