# -*- coding: utf-8 -*-
"""Visibility Doctor — Diagnose why an element is not visible in a view.

Read-only diagnostic for Revit 2024.  No transactions, no model changes.
"""
__title__ = "Visibility\nDoctor"
__doc__ = "Diagnose why an element is not visible in a view"
__author__ = "Hazem Sakr"

# ═══════════════════════════════════════════════════════════════════════
# IMPORTS
# ═══════════════════════════════════════════════════════════════════════
import clr
import os

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
clr.AddReference("System")

from Autodesk.Revit.DB import *        # noqa – wildcard is intentional for IronPython compat
from Autodesk.Revit.UI import *        # noqa
from System import Int64
from System.Collections.Generic import List as GenericList

from System.Windows import (
    Visibility as WinVisibility,
    Thickness,
    CornerRadius,
    HorizontalAlignment,
    VerticalAlignment,
    FontWeights,
    TextWrapping,
)
from System.Windows.Controls import (
    StackPanel,
    TextBlock,
    Border,
    DockPanel,
    Orientation,
    Separator,
    Dock,
)
from System.Windows.Media import SolidColorBrush, Color

from pyrevit import forms

# ═══════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

# View types for which visibility diagnosis is meaningful
GRAPHICAL_VIEW_TYPES = set()
for _vt_name in (
    "FloorPlan", "CeilingPlan", "Elevation", "Section",
    "ThreeD", "AreaPlan", "Detail", "EngineeringPlan",
):
    if hasattr(ViewType, _vt_name):
        GRAPHICAL_VIEW_TYPES.add(getattr(ViewType, _vt_name))

VIEW_TYPE_LABELS = {
    "FloorPlan":       "Floor Plan",
    "CeilingPlan":     "Ceiling Plan",
    "Elevation":       "Elevation",
    "Section":         "Section",
    "ThreeD":          "3D View",
    "AreaPlan":        "Area Plan",
    "Detail":          "Detail View",
    "EngineeringPlan": "Structural Plan",
}

# Priority ranking for CONFIRMED blockers (lower = higher priority)
# Link-level blockers rank ABOVE all element-level blockers.
BLOCKER_PRIORITY = {
    "LINK_NOT_LOADED":            0,
    "LINK_INSTANCE_HIDDEN":       1,
    "LINK_CAT_HIDDEN":            2,
    "LINK_WORKSET_HIDDEN":        3,
    "LINK_PHASE_DONT_SHOW":       4,
    "LINK_DESIGN_OPTION_MISMATCH": 5,
    "ELEM_NOT_FOUND":             10,
    "ELEM_IS_TYPE":               11,
    "ELEM_NO_CATEGORY":           12,
    "ELEM_WRONG_OWNER_VIEW":      13,
    "TEMP_HIDE_ISOLATE_ACTIVE":   14,
    "ELEM_HIDDEN":                15,
    "CAT_HIDDEN":                 16,
    "WORKSET_HIDDEN":             17,
    "FILTER_HIDDEN":              18,
    "PHASE_DONT_SHOW":            19,
    "DESIGN_OPTION_MISMATCH":     20,
    "GEOM_ABOVE_VIEW_RANGE":      21,
    "GEOM_BELOW_VIEW_RANGE":      22,
    "GEOM_OUTSIDE_CROP":          23,
    "GEOM_OUTSIDE_SECTION_BOX":   24,
    "GEOM_MASKED":                25,
}

# BuiltInParameter IDs used for view-template V/G-group mapping.
# Each entry is (group_label, bip_attr_name).  Wrapped so a missing attr
# degrades gracefully rather than crashing.
_TEMPLATE_VG_MAP = [
    ("model_categories",      "VIS_GRAPHICS_MODEL"),
    ("annotation_categories", "VIS_GRAPHICS_ANNOTATION"),
    ("analytical_categories", "VIS_GRAPHICS_ANALYT"),
    ("import_categories",     "VIS_GRAPHICS_IMPORT"),
    ("revit_links",           "VIS_GRAPHICS_RVT_LINKS"),
    ("filters",               "VIS_GRAPHICS_FILTERS"),
    ("worksets",              "VIS_GRAPHICS_WORKSETS"),
    ("phase_filter",          "VIEW_PHASE_FILTER"),
    ("phase",                 "VIEW_PHASE"),
]


# ═══════════════════════════════════════════════════════════════════════
# RESULT MODEL
# ═══════════════════════════════════════════════════════════════════════

class DiagResult(object):
    """One check result with severity and confidence on independent axes."""

    SEVERITY_INFO    = "INFO"
    SEVERITY_WARNING = "WARNING"
    SEVERITY_ERROR   = "ERROR"

    CONFIDENCE_CONFIRMED = "CONFIRMED"
    CONFIDENCE_POSSIBLE  = "POSSIBLE"
    CONFIDENCE_UNKNOWN   = "UNKNOWN"

    STATUS_PASS     = "PASS"
    STATUS_BLOCKED  = "BLOCKED"
    STATUS_POSSIBLE = "POSSIBLE"
    STATUS_UNKNOWN  = "UNKNOWN"

    def __init__(self, code, title, message, severity, confidence, source):
        self.code       = code
        self.title      = title
        self.message    = message
        self.severity   = severity
        self.confidence = confidence
        self.source     = source

    @property
    def status(self):
        if self.confidence == self.CONFIDENCE_UNKNOWN:
            return self.STATUS_UNKNOWN
        if (self.severity == self.SEVERITY_ERROR
                and self.confidence == self.CONFIDENCE_CONFIRMED):
            return self.STATUS_BLOCKED
        if (self.confidence == self.CONFIDENCE_POSSIBLE
                and self.severity in (self.SEVERITY_ERROR, self.SEVERITY_WARNING)):
            return self.STATUS_POSSIBLE
        return self.STATUS_PASS


def _pass(code, title, message, source):
    return DiagResult(code, title, message,
                      DiagResult.SEVERITY_INFO,
                      DiagResult.CONFIDENCE_CONFIRMED, source)

def _error(code, title, message, confidence, source):
    return DiagResult(code, title, message,
                      DiagResult.SEVERITY_ERROR, confidence, source)

def _warning(code, title, message, confidence, source):
    return DiagResult(code, title, message,
                      DiagResult.SEVERITY_WARNING, confidence, source)

def _unknown(code, title, message, source):
    return DiagResult(code, title, message,
                      DiagResult.SEVERITY_WARNING,
                      DiagResult.CONFIDENCE_UNKNOWN, source)


# ═══════════════════════════════════════════════════════════════════════
# ELEMENT INFO  (for the summary card)
# ═══════════════════════════════════════════════════════════════════════

def get_element_info(doc, element):
    """Return a dict of display-friendly element properties."""
    info = {"id": str(element.Id.IntegerValue)}

    if element.Category:
        info["category"] = element.Category.Name

    # Type / Family
    try:
        type_id = element.GetTypeId()
        if type_id and type_id != ElementId.InvalidElementId:
            etype = doc.GetElement(type_id)
            if etype:
                info["type"] = getattr(etype, "Name", "") or ""
                if hasattr(etype, "FamilyName"):
                    fn = etype.FamilyName
                    if fn:
                        info["family"] = fn
    except Exception:
        pass

    # Workset
    if doc.IsWorkshared:
        try:
            ws_id = element.WorksetId
            if ws_id and ws_id.IntegerValue > 0:
                ws = doc.GetWorksetTable().GetWorkset(ws_id)
                if ws:
                    info["workset"] = ws.Name
        except Exception:
            pass

    # Design option
    try:
        do = element.DesignOption
        if do:
            info["design_option"] = do.Name or str(do.Id.IntegerValue)
    except Exception:
        pass

    # Phase created / demolished
    try:
        pc = element.get_Parameter(BuiltInParameter.PHASE_CREATED)
        if pc:
            pid = pc.AsElementId()
            if pid and pid != ElementId.InvalidElementId:
                ph = doc.GetElement(pid)
                if ph:
                    info["phase_created"] = ph.Name
    except Exception:
        pass
    try:
        pd = element.get_Parameter(BuiltInParameter.PHASE_DEMOLISHED)
        if pd:
            pid = pd.AsElementId()
            if pid and pid != ElementId.InvalidElementId:
                ph = doc.GetElement(pid)
                if ph:
                    info["phase_demolished"] = ph.Name
    except Exception:
        pass

    return info


# ═══════════════════════════════════════════════════════════════════════
# DIAGNOSTIC CHECKS   (each returns a list of DiagResult)
# ═══════════════════════════════════════════════════════════════════════

# 1 ─────────────────────────────────────────────────────────────────────
def diagnose_element_exists(doc, element_id, view):
    """Check whether the element exists, is an instance, has a category,
    and is not view-specific to a different view."""
    SRC = "diagnose_element_exists"
    results = []
    element = doc.GetElement(element_id)

    if element is None:
        results.append(_error(
            "ELEM_NOT_FOUND", "Element Not Found",
            "Element ID %s was not found in the document.  "
            "The element may not exist, may be on a closed workset, "
            "or may belong to a different document (e.g. a linked model)."
            % str(element_id.IntegerValue),
            DiagResult.CONFIDENCE_CONFIRMED, SRC))
        return results, None

    # ElementType rather than instance?
    if isinstance(element, ElementType):
        tn = getattr(element, "Name", None) or ""
        results.append(_error(
            "ELEM_IS_TYPE", "Element Is a Type, Not an Instance",
            "Element ID %s is an ElementType ('%s'), not a placed instance.  "
            "Types do not appear in views directly."
            % (str(element_id.IntegerValue), tn),
            DiagResult.CONFIDENCE_CONFIRMED, SRC))
        return results, element

    # No category
    if element.Category is None:
        results.append(_warning(
            "ELEM_NO_CATEGORY", "No Category",
            "This element has no category.  It may not be a graphical element "
            "and may never be visible in any view.",
            DiagResult.CONFIDENCE_POSSIBLE, SRC))

    # View-specific element in the wrong view
    try:
        ov_id = element.OwnerViewId
        if ov_id and ov_id != ElementId.InvalidElementId and ov_id != view.Id:
            ov = doc.GetElement(ov_id)
            ov_name = ov.Name if ov else str(ov_id.IntegerValue)
            results.append(_error(
                "ELEM_WRONG_OWNER_VIEW",
                "View-Specific Element in Wrong View",
                "This element belongs to view '%s' (ID %s) and can only "
                "appear in that view.  It will never be visible in '%s'."
                % (ov_name, str(ov_id.IntegerValue), view.Name),
                DiagResult.CONFIDENCE_CONFIRMED, SRC))
    except Exception:
        pass

    # If nothing bad was found, confirm existence
    if not results:
        results.append(_pass(
            "ELEM_EXISTS", "Element Exists",
            "Element ID %s exists in the document." % str(element_id.IntegerValue),
            SRC))

    return results, element


# 2 ─────────────────────────────────────────────────────────────────────
def diagnose_element_hidden(doc, element, view):
    """Element.IsHidden and temporary hide/isolate."""
    SRC = "diagnose_element_hidden"
    results = []

    # --- permanent hide ---
    try:
        can_hide = True
        if hasattr(element, "CanBeHidden"):
            try:
                can_hide = element.CanBeHidden(view)
            except Exception:
                pass
        if can_hide:
            is_hidden = element.IsHidden(view)
            if is_hidden:
                results.append(_error(
                    "ELEM_HIDDEN", "Element Is Explicitly Hidden",
                    "The element is explicitly hidden in this view "
                    "(right-click > Hide in View > Elements).",
                    DiagResult.CONFIDENCE_CONFIRMED, SRC))
            else:
                results.append(_pass(
                    "ELEM_NOT_HIDDEN", "Element Not Explicitly Hidden",
                    "The element is not explicitly hidden in this view.",
                    SRC))
        else:
            results.append(_pass(
                "ELEM_CANNOT_HIDE", "Element Cannot Be Hidden",
                "This element type cannot be explicitly hidden in views.",
                SRC))
    except Exception as ex:
        results.append(_unknown(
            "ELEM_HIDDEN_UNKNOWN", "Element Hidden Check",
            "Unable to determine if element is explicitly hidden: %s" % str(ex),
            SRC))

    # --- temporary hide / isolate ---
    try:
        if hasattr(TemporaryViewMode, "TemporaryHideIsolate"):
            in_temp = view.IsInTemporaryViewMode(
                TemporaryViewMode.TemporaryHideIsolate)
            if in_temp:
                results.append(_error(
                    "TEMP_HIDE_ISOLATE_ACTIVE",
                    "Temporary Hide/Isolate Is Active",
                    "This view is in Temporary Hide/Isolate mode.  "
                    "Elements may be hidden or isolated.  "
                    "Reset Temporary Hide/Isolate to rule this out.",
                    DiagResult.CONFIDENCE_POSSIBLE, SRC))
    except Exception as ex:
        results.append(_unknown(
            "TEMP_HIDE_UNKNOWN", "Temporary Hide/Isolate",
            "Unable to check temporary hide/isolate: %s" % str(ex), SRC))

    return results


# 3 ─────────────────────────────────────────────────────────────────────
def diagnose_category_visibility(doc, element, view):
    """View.GetCategoryHidden + subcategory scan."""
    SRC = "diagnose_category_visibility"
    results = []

    cat = element.Category
    if cat is None:
        results.append(_unknown(
            "CAT_NO_CATEGORY", "No Category",
            "Element has no category; cannot check category visibility.", SRC))
        return results

    cat_name = cat.Name

    # Parent category
    try:
        is_hidden = view.GetCategoryHidden(cat.Id)
        if is_hidden:
            results.append(_error(
                "CAT_HIDDEN", "Category Hidden",
                "Category '%s' is turned off in the Visibility/Graphics "
                "overrides of this view." % cat_name,
                DiagResult.CONFIDENCE_CONFIRMED, SRC))
        else:
            results.append(_pass(
                "CAT_VISIBLE", "Category Visible",
                "Category '%s' is visible in the Visibility/Graphics "
                "overrides." % cat_name, SRC))
    except Exception as ex:
        results.append(_unknown(
            "CAT_UNKNOWN", "Category Visibility",
            "Unable to check category visibility for '%s': %s"
            % (cat_name, str(ex)), SRC))

    # Subcategories
    try:
        subs = cat.SubCategories
        if subs and subs.Size > 0:
            hidden_subs = []
            for sc in subs:
                try:
                    if view.GetCategoryHidden(sc.Id):
                        hidden_subs.append(sc.Name)
                except Exception:
                    pass
            if hidden_subs:
                results.append(_warning(
                    "SUBCAT_HIDDEN", "Subcategories Hidden",
                    "These subcategories of '%s' are hidden: %s.  "
                    "If the element uses geometry on a hidden subcategory "
                    "it may be partially or fully invisible."
                    % (cat_name, ", ".join(hidden_subs)),
                    DiagResult.CONFIDENCE_POSSIBLE, SRC))
    except Exception:
        pass   # subcategory iteration not critical

    return results


# 4 ─────────────────────────────────────────────────────────────────────
def diagnose_view_template(doc, element, view):
    """Detect view-template presence and which V/G groups it controls.

    Returns (results, template_info_dict).
    template_info may contain 'name' and 'controlled_groups' (a set of
    group labels such as 'model_categories', 'filters', 'worksets', ...).
    """
    SRC = "diagnose_view_template"
    results = []
    template_info = {}

    try:
        tmpl_id = view.ViewTemplateId
    except Exception:
        tmpl_id = ElementId.InvalidElementId

    if tmpl_id is None or tmpl_id == ElementId.InvalidElementId:
        results.append(_pass(
            "TEMPLATE_NONE", "No View Template",
            "No view template is applied to this view.", SRC))
        return results, template_info

    tmpl = doc.GetElement(tmpl_id)
    if tmpl is None:
        results.append(_unknown(
            "TEMPLATE_UNKNOWN", "View Template",
            "A view template is assigned (ID %s) but could not be loaded."
            % str(tmpl_id.IntegerValue), SRC))
        return results, template_info

    tmpl_name = tmpl.Name or str(tmpl_id.IntegerValue)
    template_info["name"] = tmpl_name
    template_info["controlled_groups"] = set()

    # Determine which V/G groups are controlled
    try:
        controlled_ids = set(tmpl.GetTemplateParameterIds())

        # Also try non-controlled to double-check
        non_controlled_ids = set()
        if hasattr(tmpl, "GetNonControlledTemplateParameterIds"):
            non_controlled_ids = set(
                tmpl.GetNonControlledTemplateParameterIds())

        for group_label, bip_attr in _TEMPLATE_VG_MAP:
            if not hasattr(BuiltInParameter, bip_attr):
                continue
            bip = getattr(BuiltInParameter, bip_attr)
            param_eid = ElementId(bip)
            if param_eid in controlled_ids:
                template_info["controlled_groups"].add(group_label)
            elif param_eid in non_controlled_ids:
                pass  # explicitly not controlled
            # else: can't tell — don't add

        controlled_labels = template_info["controlled_groups"]
        if controlled_labels:
            readable = ", ".join(sorted(
                lbl.replace("_", " ").title() for lbl in controlled_labels))
            results.append(_pass(
                "TEMPLATE_APPLIED", "View Template Applied",
                "View template '%s' is applied.  It controls: %s."
                % (tmpl_name, readable), SRC))
        else:
            results.append(_pass(
                "TEMPLATE_APPLIED", "View Template Applied",
                "View template '%s' is applied.  Unable to determine "
                "which V/G groups it controls." % tmpl_name, SRC))

    except Exception as ex:
        results.append(_warning(
            "TEMPLATE_PARTIAL", "View Template (Partial)",
            "View template '%s' is applied but its controlled parameters "
            "could not be enumerated: %s" % (tmpl_name, str(ex)),
            DiagResult.CONFIDENCE_UNKNOWN, SRC))

    return results, template_info


# 5 ─────────────────────────────────────────────────────────────────────
def diagnose_workset_visibility(doc, element, view):
    """Workset visibility in workshared documents."""
    SRC = "diagnose_workset_visibility"
    results = []

    if not doc.IsWorkshared:
        results.append(_pass(
            "WORKSET_NOT_WORKSHARED", "Worksets",
            "Document is not workshared; workset visibility does not apply.",
            SRC))
        return results

    try:
        ws_id = element.WorksetId
    except Exception:
        results.append(_unknown(
            "WORKSET_UNKNOWN", "Workset",
            "Unable to read the element's WorksetId.", SRC))
        return results

    if ws_id is None or ws_id.IntegerValue <= 0:
        results.append(_pass(
            "WORKSET_NONE", "Worksets",
            "Element does not belong to a user workset.", SRC))
        return results

    # Workset name and open/closed status
    ws_name = str(ws_id.IntegerValue)
    ws_open = True
    try:
        wt = doc.GetWorksetTable()
        ws = wt.GetWorkset(ws_id)
        if ws:
            ws_name = ws.Name
            ws_open = ws.IsOpen
    except Exception:
        pass

    if not ws_open:
        results.append(_warning(
            "WORKSET_CLOSED", "Workset Closed",
            "Workset '%s' is currently closed.  Elements on a closed "
            "workset are not loaded and cannot be seen." % ws_name,
            DiagResult.CONFIDENCE_CONFIRMED, SRC))

    # View-level visibility
    try:
        vis = view.GetWorksetVisibility(ws_id)

        if vis == WorksetVisibility.Hidden:
            results.append(_error(
                "WORKSET_HIDDEN", "Workset Hidden in View",
                "Workset '%s' is explicitly hidden in this view's "
                "Visibility/Graphics overrides." % ws_name,
                DiagResult.CONFIDENCE_CONFIRMED, SRC))

        elif vis == WorksetVisibility.Visible:
            results.append(_pass(
                "WORKSET_VISIBLE", "Workset Visible",
                "Workset '%s' is explicitly visible in this view." % ws_name,
                SRC))

        else:
            # UseGlobalSetting (or any other value)
            # Try to look up the default workset visibility
            default_visible = None
            try:
                # Revit 2024 API: WorksetDefaultVisibilitySettings
                if hasattr(WorksetDefaultVisibilitySettings,
                           "GetWorksetDefaultVisibilitySettings"):
                    defaults = (WorksetDefaultVisibilitySettings
                                .GetWorksetDefaultVisibilitySettings(doc))
                    default_visible = defaults.IsWorksetVisible(ws_id)
            except Exception:
                pass

            if default_visible is False:
                results.append(_error(
                    "WORKSET_HIDDEN", "Workset Hidden (Global Default)",
                    "Workset '%s' uses the global default setting, which "
                    "is set to hidden." % ws_name,
                    DiagResult.CONFIDENCE_CONFIRMED, SRC))
            elif default_visible is True:
                results.append(_pass(
                    "WORKSET_VISIBLE", "Workset Visible (Global Default)",
                    "Workset '%s' uses the global default setting, which "
                    "is set to visible." % ws_name, SRC))
            else:
                results.append(_warning(
                    "WORKSET_GLOBAL_UNKNOWN",
                    "Workset Uses Global Default",
                    "Workset '%s' uses the global default visibility "
                    "setting.  Unable to determine whether the default "
                    "is visible or hidden." % ws_name,
                    DiagResult.CONFIDENCE_POSSIBLE, SRC))

    except Exception as ex:
        results.append(_unknown(
            "WORKSET_UNKNOWN", "Workset Visibility",
            "Unable to check workset visibility for '%s': %s"
            % (ws_name, str(ex)), SRC))

    return results


# 6 ─────────────────────────────────────────────────────────────────────
def diagnose_filters(doc, element, view):
    """View filters: enabled, hidden, category match, element match."""
    SRC = "diagnose_filters"
    results = []

    try:
        filter_ids = view.GetFilters()
    except Exception as ex:
        results.append(_unknown(
            "FILTER_UNKNOWN", "View Filters",
            "Unable to retrieve view filters: %s" % str(ex), SRC))
        return results

    if not filter_ids or len(filter_ids) == 0:
        results.append(_pass(
            "FILTER_NONE", "No View Filters",
            "No filters are applied to this view.", SRC))
        return results

    blocking = []
    possible = []
    unknowns = []

    for fid in filter_ids:
        try:
            fe = doc.GetElement(fid)
            if fe is None:
                continue
            f_name = getattr(fe, "Name", None) or str(fid.IntegerValue)

            # Enabled?
            is_enabled = True
            try:
                if hasattr(view, "GetIsFilterEnabled"):
                    is_enabled = view.GetIsFilterEnabled(fid)
            except Exception:
                pass
            if not is_enabled:
                continue   # disabled filters hide nothing

            # Visibility flag (False = filter hides matching elements)
            is_visible = True
            try:
                is_visible = view.GetFilterVisibility(fid)
            except Exception:
                pass
            if is_visible:
                continue   # filter does not hide

            # --- filter is enabled and hides elements ---

            # Category match?
            cat_match = None   # None = couldn't determine
            try:
                if hasattr(fe, "GetCategories"):
                    f_cats = fe.GetCategories()
                    if element.Category:
                        cat_match = (element.Category.Id in f_cats)
                    else:
                        cat_match = None
            except Exception:
                cat_match = None

            if cat_match is False:
                continue   # element's category is not in the filter

            # Element passes filter?
            passes = None
            try:
                ef = None
                if hasattr(fe, "GetElementFilter"):
                    ef = fe.GetElementFilter()
                if ef is not None:
                    # Try direct PassesFilter first
                    try:
                        passes = ef.PassesFilter(element)
                    except Exception:
                        pass
                    if passes is None:
                        try:
                            passes = ef.PassesFilter(doc, element.Id)
                        except Exception:
                            pass
                    # Fallback: tiny collector
                    if passes is None:
                        try:
                            ids = GenericList[ElementId]()
                            ids.Add(element.Id)
                            c = FilteredElementCollector(doc, ids)
                            c.WherePasses(ef)
                            passes = (c.GetElementCount() > 0)
                        except Exception:
                            pass
                else:
                    # Might be a selection filter
                    if hasattr(fe, "GetElementIds"):
                        try:
                            sel_ids = fe.GetElementIds()
                            passes = (element.Id in sel_ids)
                        except Exception:
                            pass
            except Exception:
                passes = None

            if passes is True:
                blocking.append(f_name)
            elif passes is False:
                pass   # element doesn't match; no issue
            else:
                possible.append(f_name)

        except Exception as ex:
            unknowns.append(str(ex))

    # Build results
    for fn in blocking:
        results.append(_error(
            "FILTER_HIDDEN",
            "Hidden by Filter '%s'" % fn,
            "View filter '%s' is enabled, set to hide matching elements, "
            "and this element matches the filter criteria." % fn,
            DiagResult.CONFIDENCE_CONFIRMED, SRC))

    for fn in possible:
        results.append(_warning(
            "FILTER_POSSIBLE",
            "Possibly Hidden by Filter '%s'" % fn,
            "View filter '%s' is enabled and hides elements in a matching "
            "category, but unable to confirm whether the element meets "
            "the filter criteria." % fn,
            DiagResult.CONFIDENCE_POSSIBLE, SRC))

    for msg in unknowns:
        results.append(_unknown(
            "FILTER_UNKNOWN", "Filter Evaluation Error",
            "A filter could not be evaluated: %s" % msg, SRC))

    if not results:
        results.append(_pass(
            "FILTER_OK", "View Filters OK",
            "No view filters are hiding this element "
            "(%d filter(s) evaluated)." % len(filter_ids), SRC))

    return results


# 7 ─────────────────────────────────────────────────────────────────────
def diagnose_phasing(doc, element, view):
    """Phase status vs. phase-filter presentation."""
    SRC = "diagnose_phasing"
    results = []

    # Does element support phasing?
    pc_param = None
    try:
        pc_param = element.get_Parameter(BuiltInParameter.PHASE_CREATED)
    except Exception:
        pass
    if pc_param is None:
        results.append(_pass(
            "PHASE_NA", "Phasing Not Applicable",
            "This element does not carry phase information.", SRC))
        return results

    try:
        # View's phase
        vp_param = view.get_Parameter(BuiltInParameter.VIEW_PHASE)
        if vp_param is None:
            results.append(_unknown(
                "PHASE_UNKNOWN", "Phasing",
                "Unable to read the view's Phase parameter.", SRC))
            return results
        vp_id = vp_param.AsElementId()
        if vp_id is None or vp_id == ElementId.InvalidElementId:
            results.append(_unknown(
                "PHASE_UNKNOWN", "Phasing",
                "View does not have a valid phase.", SRC))
            return results

        vp_elem = doc.GetElement(vp_id)
        vp_name = vp_elem.Name if vp_elem else str(vp_id.IntegerValue)

        # Element's phase status in the view's phase
        status = element.GetPhaseStatus(vp_id)
        status_name = str(status).split(".")[-1]   # e.g. "Past"

        # View's phase filter
        vpf_param = view.get_Parameter(BuiltInParameter.VIEW_PHASE_FILTER)
        if vpf_param is None:
            results.append(_unknown(
                "PHASE_UNKNOWN", "Phasing",
                "Element phase status is '%s' in phase '%s', but the "
                "view's Phase Filter parameter could not be read."
                % (status_name, vp_name), SRC))
            return results

        vpf_id = vpf_param.AsElementId()
        pf = doc.GetElement(vpf_id) if vpf_id != ElementId.InvalidElementId else None
        pf_name = pf.Name if pf else str(vpf_id.IntegerValue)

        if pf and hasattr(pf, "GetPhaseStatusPresentation"):
            presentation = pf.GetPhaseStatusPresentation(status)
            pres_str = str(presentation).split(".")[-1]

            if hasattr(PhaseStatusPresentation, "DontShow"):
                dont_show = PhaseStatusPresentation.DontShow
            else:
                dont_show = None

            if dont_show is not None and presentation == dont_show:
                results.append(_error(
                    "PHASE_DONT_SHOW", "Phase: Not Displayed",
                    "Element phase status is '%s' in view phase '%s'.  "
                    "Phase filter '%s' sets this status to 'Not Displayed'."
                    % (status_name, vp_name, pf_name),
                    DiagResult.CONFIDENCE_CONFIRMED, SRC))
            else:
                results.append(_pass(
                    "PHASE_OK", "Phasing OK",
                    "Element phase status is '%s' in phase '%s'.  "
                    "Phase filter '%s' presentation: %s."
                    % (status_name, vp_name, pf_name, pres_str), SRC))
        else:
            results.append(_unknown(
                "PHASE_UNKNOWN", "Phasing",
                "Element phase status is '%s' in phase '%s'.  "
                "Unable to evaluate the phase-filter presentation."
                % (status_name, vp_name), SRC))

    except Exception as ex:
        results.append(_unknown(
            "PHASE_UNKNOWN", "Phasing",
            "Unable to evaluate phasing: %s" % str(ex), SRC))

    return results


# 8 ─────────────────────────────────────────────────────────────────────
def diagnose_design_option(doc, element, view):
    """Design-option visibility."""
    SRC = "diagnose_design_option"
    results = []

    try:
        elem_opt = element.DesignOption
    except Exception:
        elem_opt = None

    if elem_opt is None:
        results.append(_pass(
            "DESIGN_OPTION_NA", "Design Option",
            "Element is not part of any design option.", SRC))
        return results

    opt_name = getattr(elem_opt, "Name", None) or str(elem_opt.Id.IntegerValue)

    # Determine whether the view shows this option
    try:
        bip_attr = "VIEWER_OPTION_VISIBILITY"
        if not hasattr(BuiltInParameter, bip_attr):
            results.append(_unknown(
                "DESIGN_OPTION_UNKNOWN", "Design Option",
                "Element is in design option '%s'.  Unable to read the "
                "view's design-option parameter (BuiltInParameter.%s "
                "not found)." % (opt_name, bip_attr), SRC))
            return results

        bip = getattr(BuiltInParameter, bip_attr)
        vo_param = view.get_Parameter(bip)
        if vo_param is None:
            results.append(_unknown(
                "DESIGN_OPTION_UNKNOWN", "Design Option",
                "Element is in design option '%s'.  The view does not "
                "expose a design-option parameter." % opt_name, SRC))
            return results

        vo_id = vo_param.AsElementId()
        # InvalidElementId / negative → automatic (show primary option of each set)
        if vo_id is None or vo_id == ElementId.InvalidElementId or vo_id.IntegerValue < 0:
            # View is in "Automatic" mode — only primary options shown
            is_primary = False
            try:
                if hasattr(elem_opt, "IsPrimary"):
                    is_primary = elem_opt.IsPrimary
            except Exception:
                pass

            if is_primary:
                results.append(_pass(
                    "DESIGN_OPTION_OK", "Design Option OK",
                    "Element is in the primary design option '%s'; "
                    "the view uses automatic option display." % opt_name,
                    SRC))
            else:
                results.append(_error(
                    "DESIGN_OPTION_MISMATCH",
                    "Design Option May Be Hidden",
                    "Element is in design option '%s', which may not be "
                    "the primary option.  The view uses automatic option "
                    "display, which typically shows only primary options."
                    % opt_name,
                    DiagResult.CONFIDENCE_POSSIBLE, SRC))
            return results

        # View explicitly names an option
        if vo_id == elem_opt.Id:
            results.append(_pass(
                "DESIGN_OPTION_OK", "Design Option OK",
                "The view explicitly shows design option '%s'." % opt_name,
                SRC))
        else:
            vo_elem = doc.GetElement(vo_id)
            vo_name = vo_elem.Name if vo_elem else str(vo_id.IntegerValue)
            results.append(_error(
                "DESIGN_OPTION_MISMATCH",
                "Design Option Mismatch",
                "Element is in design option '%s' but the view is set to "
                "show '%s'." % (opt_name, vo_name),
                DiagResult.CONFIDENCE_POSSIBLE, SRC))

    except Exception as ex:
        results.append(_unknown(
            "DESIGN_OPTION_UNKNOWN", "Design Option",
            "Unable to evaluate design-option visibility: %s" % str(ex),
            SRC))

    return results


# 9 ─────────────────────────────────────────────────────────────────────
def _get_effective_bounding_box(element, view, link_transform=None):
    """Get the element's bounding box, applying link transform if present."""
    if link_transform:
        # Linked element: get its local BB and transform the 8 corners
        bb = element.get_BoundingBox(None)
        if bb is None:
            return None
        corners = [
            XYZ(bb.Min.X, bb.Min.Y, bb.Min.Z),
            XYZ(bb.Max.X, bb.Min.Y, bb.Min.Z),
            XYZ(bb.Min.X, bb.Max.Y, bb.Min.Z),
            XYZ(bb.Max.X, bb.Max.Y, bb.Min.Z),
            XYZ(bb.Min.X, bb.Min.Y, bb.Max.Z),
            XYZ(bb.Max.X, bb.Min.Y, bb.Max.Z),
            XYZ(bb.Min.X, bb.Max.Y, bb.Max.Z),
            XYZ(bb.Max.X, bb.Max.Y, bb.Max.Z),
        ]
        t_corners = [link_transform.OfPoint(c) for c in corners]
        xs = [c.X for c in t_corners]
        ys = [c.Y for c in t_corners]
        zs = [c.Z for c in t_corners]
        new_bb = BoundingBoxXYZ()
        new_bb.Min = XYZ(min(xs), min(ys), min(zs))
        new_bb.Max = XYZ(max(xs), max(ys), max(zs))
        return new_bb
    else:
        # Host element
        bb = element.get_BoundingBox(view)
        if bb is None:
            bb = element.get_BoundingBox(None)
        return bb

def diagnose_geometry_visibility(doc, element, view, link_transform=None):
    """Check view range and crop region vs element bounding box.

    This is the geometric fallback — called when all settings checks pass.
    Only meaningful for plan views (FloorPlan, CeilingPlan, AreaPlan) and
    sections / elevations where a PlanViewRange or crop region is active.
    3D views use section boxes, which are noted separately.
    """
    SRC = "diagnose_geometry_visibility"
    results = []

    # --- Bounding box of the element in the view ---
    try:
        bb = _get_effective_bounding_box(element, view, link_transform)
        if bb is None:
            results.append(_unknown(
                "GEOM_BB_UNKNOWN", "Geometry / Bounding Box",
                "Unable to retrieve the element's bounding box.  "
                "The element may have no visible geometry in any view.",
                SRC))
            return results
        elem_min_z = bb.Min.Z
        elem_max_z = bb.Max.Z
    except Exception as ex:
        results.append(_unknown(
            "GEOM_BB_UNKNOWN", "Geometry / Bounding Box",
            "Unable to retrieve bounding box: %s" % str(ex), SRC))
        return results

    # --- Plan view range ---
    plan_types = [
        "FloorPlan", "CeilingPlan", "AreaPlan",
    ]
    vtype_str = str(view.ViewType).split(".")[-1]

    # Skip view range checks for elements that don't respect Z cut planes
    cat = element.Category
    cat_name = cat.Name.lower() if cat else ""
    is_infinite = ("property line" in cat_name or "grids" in cat_name)
    
    if is_infinite:
        results.append(_pass(
            "GEOM_VIEW_RANGE_SKIPPED", "View Range",
            "View range checks are skipped for this category (e.g. Property Lines) because they project infinitely.", SRC))
    elif hasattr(view, "GetViewRange") and vtype_str in plan_types:
        try:
            vr = view.GetViewRange()
            if vr is not None:
                # Convert offsets to absolute elevations.
                # PlanViewPlane: 0=TopClip, 2=CutPlane, 3=BottomClip, 4=ViewDepth
                TOP_CLIP     = 0  # PlanViewPlane.TopClipPlane
                CUT          = 2  # PlanViewPlane.CutPlane
                BOTTOM_CLIP  = 3  # PlanViewPlane.BottomClipPlane
                VIEW_DEPTH   = 4  # PlanViewPlane.ViewDepthPlane

                def _offset_z(plane_id):
                    try:
                        level_id = vr.GetLevelId(plane_id)
                        offset   = vr.GetOffset(plane_id)
                        if level_id and level_id != ElementId.InvalidElementId:
                            lvl = doc.GetElement(level_id)
                            if lvl:
                                return lvl.Elevation + offset
                        return offset
                    except Exception:
                        return None

                cut_z    = _offset_z(CUT)
                top_z    = _offset_z(TOP_CLIP)
                bottom_z = _offset_z(BOTTOM_CLIP)
                depth_z  = _offset_z(VIEW_DEPTH)

                lines = []
                if cut_z is not None:
                    lines.append(u"  Cut plane:    %.0f mm" % (cut_z * 304.8))
                if top_z is not None:
                    lines.append(u"  Top clip:     %.0f mm" % (top_z * 304.8))
                if bottom_z is not None:
                    lines.append(u"  Bottom clip:  %.0f mm" % (bottom_z * 304.8))
                if depth_z is not None:
                    lines.append(u"  View depth:   %.0f mm" % (depth_z * 304.8))
                lines.append(u"  Element Z:    %.0f – %.0f mm"
                             % (elem_min_z * 304.8, elem_max_z * 304.8))

                # A plan element is visible in projected view if it sits
                # between view-depth and top-clip.  If its bb is entirely
                # above top_z or entirely below depth_z it won't show.
                blocked_above = (top_z is not None
                                 and elem_min_z > top_z)
                blocked_below = (depth_z is not None
                                 and elem_max_z < depth_z)
                blocked_cut   = (cut_z is not None
                                 and bottom_z is not None
                                 and top_z is not None
                                 and (elem_min_z > top_z
                                      or elem_max_z < bottom_z))

                summary = u"\n".join(lines)

                if blocked_above:
                    results.append(_error(
                        "GEOM_ABOVE_VIEW_RANGE",
                        "Element Above View Range",
                        u"The element's bounding box (min Z = %.0f mm) sits "
                        u"ABOVE the view's top clip plane (%.0f mm).  "
                        u"Raise the Top Clip Plane in View Range settings "
                        u"or lower the element.\n\n%s"
                        % (elem_min_z * 304.8, top_z * 304.8, summary),
                        DiagResult.CONFIDENCE_CONFIRMED, SRC))
                elif blocked_below:
                    results.append(_error(
                        "GEOM_BELOW_VIEW_RANGE",
                        "Element Below View Depth",
                        u"The element's bounding box (max Z = %.0f mm) sits "
                        u"BELOW the view depth (%.0f mm).  "
                        u"Lower the View Depth in View Range settings "
                        u"or raise the element.\n\n%s"
                        % (elem_max_z * 304.8, depth_z * 304.8, summary),
                        DiagResult.CONFIDENCE_CONFIRMED, SRC))
                else:
                    results.append(_pass(
                        "GEOM_VIEW_RANGE_OK",
                        "View Range",
                        u"Element Z range (%.0f – %.0f mm) appears within "
                        u"the view range.  If still invisible, the element "
                        u"may be a 3D family whose geometry does not "
                        u"intersect the cut plane (%.0f mm).\n\n%s"
                        % (elem_min_z * 304.8, elem_max_z * 304.8,
                           (cut_z if cut_z is not None else 0) * 304.8,
                           summary),
                        SRC))
        except Exception as ex:
            results.append(_unknown(
                "GEOM_VIEWRANGE_UNKNOWN", "View Range",
                "Unable to evaluate view range: %s" % str(ex), SRC))
    elif vtype_str == "ThreeD":
        # Section box check for 3D views
        try:
            if hasattr(view, "IsSectionBoxActive") and view.IsSectionBoxActive:
                sb = view.GetSectionBox()
                if sb is not None:
                    sb_min = sb.Min
                    sb_max = sb.Max
                    inside = (elem_min_z <= sb_max.Z and elem_max_z >= sb_min.Z)
                    if not inside:
                        results.append(_error(
                            "GEOM_OUTSIDE_SECTION_BOX",
                            "Outside Section Box",
                            u"The element (Z: %.0f – %.0f mm) is outside the "
                            u"3D view's section box (Z: %.0f – %.0f mm)."
                            % (elem_min_z * 304.8, elem_max_z * 304.8, sb_min.Z * 304.8, sb_max.Z * 304.8),
                            DiagResult.CONFIDENCE_CONFIRMED, SRC))
                    else:
                        results.append(_pass(
                            "GEOM_SECTION_BOX_OK", "Section Box",
                            u"Element is within the section box Z range.", SRC))
            else:
                results.append(_pass(
                    "GEOM_NO_SECTION_BOX", "Section Box",
                    "No active section box on this 3D view.", SRC))
        except Exception as ex:
            results.append(_unknown(
                "GEOM_SECTIONBOX_UNKNOWN", "Section Box",
                "Unable to evaluate section box: %s" % str(ex), SRC))

    # --- Crop region ---
    try:
        if view.CropBoxActive:
            cr = view.CropBox
            bb_v = element.get_BoundingBox(view)
            if bb_v is not None:
                # Crop box min/max are in view coordinates
                outside = (bb_v.Max.X < cr.Min.X or bb_v.Min.X > cr.Max.X
                           or bb_v.Max.Y < cr.Min.Y or bb_v.Min.Y > cr.Max.Y)
                if outside:
                    results.append(_error(
                        "GEOM_OUTSIDE_CROP",
                        "Outside Crop Region",
                        u"The element's bounding box lies entirely outside "
                        u"the view's active crop region.",
                        DiagResult.CONFIDENCE_CONFIRMED, SRC))
                else:
                    results.append(_pass(
                        "GEOM_CROP_OK", "Crop Region",
                        u"Element is within the active crop region.", SRC))
            else:
                results.append(_warning(
                    "GEOM_CROP_POSSIBLE", "Crop Region",
                    u"The view has an active crop region but the element "
                    u"bounding box could not be obtained in view coordinates.",
                    DiagResult.CONFIDENCE_POSSIBLE, SRC))
        else:
            results.append(_pass(
                "GEOM_NO_CROP", "Crop Region",
                "No active crop region on this view.", SRC))
    except Exception as ex:
        results.append(_unknown(
            "GEOM_CROP_UNKNOWN", "Crop Region",
            "Unable to evaluate crop region: %s" % str(ex), SRC))

    return results


# 10 ────────────────────────────────────────────────────────────────────
def diagnose_masking_occlusion(doc, element, view, link_transform=None):
    """Check for 2D masking regions or detail items drawn over the element."""
    SRC = "diagnose_masking_occlusion"
    results = []

    # Masking only applies to projection views where 2D elements draw on top
    vtype_str = str(view.ViewType).split(".")[-1]
    if vtype_str not in ["FloorPlan", "CeilingPlan", "Elevation", "Section", "Detail", "AreaPlan"]:
        return results

    try:
        bb = _get_effective_bounding_box(element, view, link_transform)
        if bb is None:
            return results

        # Create outline for intersection
        # Revit Outline requires Min to be strictly less than Max
        min_x, max_x = min(bb.Min.X, bb.Max.X), max(bb.Min.X, bb.Max.X)
        min_y, max_y = min(bb.Min.Y, bb.Max.Y), max(bb.Min.Y, bb.Max.Y)
        
        # Add a tiny tolerance so flat bounding boxes don't crash Outline
        if max_x - min_x < 0.001: max_x += 0.001
        if max_y - min_y < 0.001: max_y += 0.001

        # For masking in plan/RCP views, we only care about X/Y overlap. 
        # The 2D detail item might be drawn at the view's cut plane Z, 
        # while the element could be at Z=0. Make the outline infinitely tall.
        min_z, max_z = -100000.0, 100000.0

        outline = Outline(XYZ(min_x, min_y, min_z), XYZ(max_x, max_y, max_z))
        bb_filter = BoundingBoxIntersectsFilter(outline)

        # Collect FilledRegions (which include Masking Regions) in the view
        # We also collect DetailComponents in case they are nested families with masking
        col_fr = FilteredElementCollector(doc, view.Id).OfClass(FilledRegion).WherePasses(bb_filter)
        
        detail_cat_id = ElementId(BuiltInCategory.OST_DetailComponents)
        col_dc = FilteredElementCollector(doc, view.Id).OfCategory(BuiltInCategory.OST_DetailComponents).WherePasses(bb_filter)
        
        # Combine them safely (avoiding LINQ Union to keep it pure Python)
        detail_items = list(col_fr.ToElements())
        fr_ids = set(e.Id.IntegerValue for e in detail_items)
        
        for dc in col_dc.ToElements():
            if dc.Id.IntegerValue not in fr_ids:
                detail_items.append(dc)

        masking_elements = []
        possible_masking = []
        
        for d in detail_items:
            # Exclude the element itself if it happens to be a detail item
            if d.Id == element.Id:
                continue
                
            is_masking = False
            desc = "Detail Item"
            
            # Identify if it's a FilledRegion
            if "FilledRegion" in d.GetType().Name:
                desc = "Filled Region"
                if hasattr(d, "GetTypeId"):
                    type_id = d.GetTypeId()
                    if type_id != ElementId.InvalidElementId:
                        elem_type = doc.GetElement(type_id)
                        if elem_type:
                            # Check IsMasking property natively
                            if hasattr(elem_type, "IsMasking") and elem_type.IsMasking:
                                is_masking = True
                            else:
                                t_name = ""
                                try:
                                    t_name = getattr(elem_type, "Name", "")
                                except Exception:
                                    pass
                                if t_name and "mask" in t_name.lower():
                                    is_masking = True
                            
                            # If it's a filled region but not explicitly masking, 
                            # it could still occlude if solid
                            if not is_masking:
                                possible_masking.append((d, desc))
                                continue
                            
            if is_masking:
                masking_elements.append((d, desc))
            else:
                possible_masking.append((d, desc))

        if masking_elements:
            lines = []
            for d, desc in masking_elements:
                try:
                    d_name = getattr(d, "Name", str(d.Id.IntegerValue))
                except Exception:
                    d_name = str(d.Id.IntegerValue)
                lines.append(u"  - ID %s: %s '%s'" % (str(d.Id.IntegerValue), desc, d_name))
                
            results.append(_error(
                "GEOM_MASKED", 
                "Occluded by Masking Region",
                u"The element's bounding box intersects with view-specific Masking Regions drawn in this view. Because 2D detailing draws on top of 3D geometry in plan/section views, the element is physically covered.\n\nMasking elements detected:\n%s" % u"\n".join(lines),
                DiagResult.CONFIDENCE_CONFIRMED, SRC))
        elif possible_masking:
            results.append(_warning(
                "GEOM_DETAIL_OVERLAP", 
                "Detail Component Overlap",
                u"There are %d view-specific Detail Item(s) overlapping the element's bounding box. Detail items draw in front of the model and might contain masking regions or solid hatches that occlude the element." % len(possible_masking),
                DiagResult.CONFIDENCE_POSSIBLE, SRC))
        else:
            results.append(_pass(
                "GEOM_NO_MASKING", "Masking Regions",
                u"No masking regions or view-specific detail items are covering the element.", SRC))
                
    except Exception as ex:
        results.append(_unknown(
            "GEOM_MASKING_UNKNOWN", "Masking Regions",
            "Unable to evaluate masking region occlusion: %s" % str(ex), SRC))

    return results


# ═══════════════════════════════════════════════════════════════════════
# LINK DIAGNOSIS  (Pass 2)
# ═══════════════════════════════════════════════════════════════════════
# NOTE (out-of-scope geometry hook):
#   link_instance.GetTotalTransform() must be applied before any future
#   geometry / bounding-box comparison between linked and host elements.
#   This pass performs no geometry checks.

# L1 ────────────────────────────────────────────────────────────────────
def diagnose_link_status(host_doc, link_instance, element_id, view):
    """Check that the link is loaded, get linked doc, resolve element.

    Returns (results, link_doc, linked_element).
    link_doc and linked_element are None when the link is not usable.
    """
    SRC = "diagnose_link_status"
    results = []
    link_doc = None
    linked_element = None

    li_name = getattr(link_instance, "Name", "") or str(link_instance.Id.IntegerValue)

    # --- Is the RevitLinkType loaded? ---
    try:
        type_id = link_instance.GetTypeId()
        link_type = host_doc.GetElement(type_id) if type_id != ElementId.InvalidElementId else None
        if link_type is not None:
            # Try RevitLinkType.IsLoaded first (takes the local Revit server)
            is_loaded = True  # assume loaded until we prove otherwise
            try:
                if hasattr(link_type, "IsLoaded"):
                    # RevitLinkType.IsLoaded(Document hostDoc, ElementId linkTypeId)
                    # or RevitLinkType.IsLoaded — property vs method varies;
                    # try the static form first, then property.
                    try:
                        is_loaded = RevitLinkType.IsLoaded(host_doc, type_id)
                    except Exception:
                        try:
                            is_loaded = link_type.IsLoaded
                        except Exception:
                            pass
            except Exception:
                pass

            if not is_loaded:
                results.append(_error(
                    "LINK_NOT_LOADED", "Link Not Loaded",
                    "The linked file '%s' is not loaded.  "
                    "No elements inside the link can be seen until it is "
                    "reloaded." % li_name,
                    DiagResult.CONFIDENCE_CONFIRMED, SRC))
                return results, None, None
    except Exception:
        pass  # continue and try GetLinkDocument

    # --- Get the linked Document ---
    try:
        link_doc = link_instance.GetLinkDocument()
    except Exception:
        link_doc = None

    if link_doc is None:
        results.append(_error(
            "LINK_NOT_LOADED", "Link Document Unavailable",
            "The linked model '%s' returned no Document.  "
            "It may be unloaded, not found, or failed to open." % li_name,
            DiagResult.CONFIDENCE_CONFIRMED, SRC))
        return results, None, None

    results.append(_pass(
        "LINK_LOADED", "Link Is Loaded",
        "Linked model '%s' is loaded.  Document: '%s'."
        % (li_name, link_doc.Title or ""), SRC))

    # --- Resolve element in the linked doc ---
    linked_element = link_doc.GetElement(element_id)
    if linked_element is None:
        # --- Nested link guard ---
        try:
            nested = FilteredElementCollector(link_doc).OfClass(RevitLinkInstance)
            if nested.GetElementCount() > 0:
                results.append(_warning(
                    "LINK_NESTED_WARN", "Nested Links Detected",
                    "The element was not found in the primary link, and nested links exist. "
                    "Nested-link diagnosis is not supported.",
                    DiagResult.CONFIDENCE_POSSIBLE, SRC))
        except Exception:
            pass

        results.append(_error(
            "ELEM_NOT_FOUND", "Element Not Found in Link",
            "Element ID %s was not found in the linked document '%s'.  "
            "It may not exist, may be on a closed workset in the link, "
            "or you may have selected the wrong link instance."
            % (str(element_id.IntegerValue), link_doc.Title or li_name),
            DiagResult.CONFIDENCE_CONFIRMED, SRC))
        return results, link_doc, None

    if isinstance(linked_element, ElementType):
        tn = getattr(linked_element, "Name", None) or ""
        results.append(_error(
            "ELEM_IS_TYPE", "Element Is a Type in the Link",
            "Element ID %s in the linked document is an ElementType ('%s'), "
            "not a placed instance." % (str(element_id.IntegerValue), tn),
            DiagResult.CONFIDENCE_CONFIRMED, SRC))
        return results, link_doc, linked_element

    results.append(_pass(
        "ELEM_FOUND_IN_LINK", "Element Found in Link",
        "Element ID %s found in linked document '%s'."
        % (str(element_id.IntegerValue), link_doc.Title or ""), SRC))

    return results, link_doc, linked_element


# L2 ────────────────────────────────────────────────────────────────────
def diagnose_link_instance_visibility(host_doc, link_instance, view):
    """Host-side checks on the RevitLinkInstance itself.

    Checks: instance hidden, OST_RvtLinks category, workset, template
    control, phase, design option of the *link instance* in the host view.
    """
    SRC = "diagnose_link_instance_visibility"
    results = []

    li_name = getattr(link_instance, "Name", "") or str(link_instance.Id.IntegerValue)

    # --- Instance explicitly hidden ---
    try:
        can_hide = True
        if hasattr(link_instance, "CanBeHidden"):
            try:
                can_hide = link_instance.CanBeHidden(view)
            except Exception:
                pass
        if can_hide:
            if link_instance.IsHidden(view):
                results.append(_error(
                    "LINK_INSTANCE_HIDDEN",
                    "Link Instance Is Hidden",
                    "The link instance '%s' is explicitly hidden in this "
                    "view (right-click > Hide in View > Elements)." % li_name,
                    DiagResult.CONFIDENCE_CONFIRMED, SRC))
            else:
                results.append(_pass(
                    "LINK_INSTANCE_VISIBLE",
                    "Link Instance Not Hidden",
                    "The link instance is not explicitly hidden.", SRC))
    except Exception as ex:
        results.append(_unknown(
            "LINK_INSTANCE_UNKNOWN", "Link Instance Hidden Check",
            "Unable to check if the link instance is hidden: %s" % str(ex),
            SRC))

    # --- OST_RvtLinks category hidden ---
    try:
        rvt_links_cat_id = None
        if hasattr(BuiltInCategory, "OST_RvtLinks"):
            rvt_links_cat_id = ElementId(BuiltInCategory.OST_RvtLinks)
        if rvt_links_cat_id is not None:
            if view.GetCategoryHidden(rvt_links_cat_id):
                results.append(_error(
                    "LINK_CAT_HIDDEN",
                    "Revit Links Category Hidden",
                    "The 'Revit Links' category is turned off in the "
                    "Visibility/Graphics overrides of this view.  "
                    "All linked models are invisible.",
                    DiagResult.CONFIDENCE_CONFIRMED, SRC))
            else:
                results.append(_pass(
                    "LINK_CAT_VISIBLE",
                    "Revit Links Category Visible",
                    "The 'Revit Links' category is visible.", SRC))
        else:
            results.append(_unknown(
                "LINK_CAT_UNKNOWN", "Revit Links Category",
                "Unable to locate BuiltInCategory.OST_RvtLinks.", SRC))
    except Exception as ex:
        results.append(_unknown(
            "LINK_CAT_UNKNOWN", "Revit Links Category",
            "Unable to check Revit Links category: %s" % str(ex), SRC))

    # --- Link instance workset ---
    if host_doc.IsWorkshared:
        try:
            ws_id = link_instance.WorksetId
            if ws_id and ws_id.IntegerValue > 0:
                ws_name = str(ws_id.IntegerValue)
                try:
                    ws = host_doc.GetWorksetTable().GetWorkset(ws_id)
                    if ws:
                        ws_name = ws.Name
                except Exception:
                    pass

                try:
                    vis = view.GetWorksetVisibility(ws_id)
                    if vis == WorksetVisibility.Hidden:
                        results.append(_error(
                            "LINK_WORKSET_HIDDEN",
                            "Link Instance Workset Hidden",
                            "The link instance's workset '%s' is hidden "
                            "in this view." % ws_name,
                            DiagResult.CONFIDENCE_CONFIRMED, SRC))
                    elif vis == WorksetVisibility.Visible:
                        results.append(_pass(
                            "LINK_WORKSET_OK",
                            "Link Instance Workset Visible",
                            "Workset '%s' is visible." % ws_name, SRC))
                    else:
                        # UseGlobalSetting
                        default_visible = None
                        try:
                            if hasattr(WorksetDefaultVisibilitySettings,
                                       "GetWorksetDefaultVisibilitySettings"):
                                defs = (WorksetDefaultVisibilitySettings
                                        .GetWorksetDefaultVisibilitySettings(
                                            host_doc))
                                default_visible = defs.IsWorksetVisible(ws_id)
                        except Exception:
                            pass
                        if default_visible is False:
                            results.append(_error(
                                "LINK_WORKSET_HIDDEN",
                                "Link Workset Hidden (Global Default)",
                                "Workset '%s' uses the global default, "
                                "which is hidden." % ws_name,
                                DiagResult.CONFIDENCE_CONFIRMED, SRC))
                        elif default_visible is True:
                            results.append(_pass(
                                "LINK_WORKSET_OK",
                                "Link Workset Visible (Global Default)",
                                "Workset '%s' uses the global default, "
                                "which is visible." % ws_name, SRC))
                        else:
                            results.append(_warning(
                                "LINK_WORKSET_POSSIBLE",
                                "Link Workset (Global Default Unknown)",
                                "Workset '%s' uses the global default.  "
                                "Unable to determine the default." % ws_name,
                                DiagResult.CONFIDENCE_POSSIBLE, SRC))
                except Exception as ex:
                    results.append(_unknown(
                        "LINK_WORKSET_UNKNOWN", "Link Instance Workset",
                        "Unable to check workset visibility: %s" % str(ex),
                        SRC))
        except Exception:
            pass  # no workset — fine

    # --- View template control over Revit Links V/G ---
    try:
        tmpl_id = view.ViewTemplateId
        if tmpl_id and tmpl_id != ElementId.InvalidElementId:
            tmpl = host_doc.GetElement(tmpl_id)
            if tmpl:
                tmpl_name = tmpl.Name or str(tmpl_id.IntegerValue)
                try:
                    ctrl_ids = set(tmpl.GetTemplateParameterIds())
                    if hasattr(BuiltInParameter, "VIS_GRAPHICS_RVT_LINKS"):
                        bip = BuiltInParameter.VIS_GRAPHICS_RVT_LINKS
                        if ElementId(bip) in ctrl_ids:
                            results.append(_pass(
                                "LINK_TEMPLATE_CONTROLS_LINKS",
                                "Template Controls Revit Links V/G",
                                "View template '%s' controls the Revit Links "
                                "visibility group." % tmpl_name, SRC))
                except Exception:
                    pass
    except Exception:
        pass

    # --- Phase of the link instance ---
    try:
        pc = link_instance.get_Parameter(BuiltInParameter.PHASE_CREATED)
        if pc is not None:
            vp_param = view.get_Parameter(BuiltInParameter.VIEW_PHASE)
            if vp_param:
                vp_id = vp_param.AsElementId()
                if vp_id and vp_id != ElementId.InvalidElementId:
                    status = link_instance.GetPhaseStatus(vp_id)
                    status_name = str(status).split(".")[-1]
                    vpf_param = view.get_Parameter(
                        BuiltInParameter.VIEW_PHASE_FILTER)
                    if vpf_param:
                        vpf_id = vpf_param.AsElementId()
                        pf = host_doc.GetElement(vpf_id) if vpf_id != ElementId.InvalidElementId else None
                        if pf and hasattr(pf, "GetPhaseStatusPresentation"):
                            pres = pf.GetPhaseStatusPresentation(status)
                            if (hasattr(PhaseStatusPresentation, "DontShow")
                                    and pres == PhaseStatusPresentation.DontShow):
                                results.append(_error(
                                    "LINK_PHASE_DONT_SHOW",
                                    "Link Instance Phase: Not Displayed",
                                    "Link instance phase status is '%s'.  "
                                    "Phase filter sets it to 'Not Displayed'."
                                    % status_name,
                                    DiagResult.CONFIDENCE_CONFIRMED, SRC))
    except Exception:
        pass  # phase on link instances is not always applicable

    # --- Design option of the link instance ---
    try:
        li_opt = link_instance.DesignOption
        if li_opt is not None:
            opt_name = getattr(li_opt, "Name", None) or str(li_opt.Id.IntegerValue)
            results.append(_warning(
                "LINK_DESIGN_OPTION",
                "Link Instance in Design Option",
                "The link instance is in design option '%s'.  "
                "If the view does not show this option, the entire link "
                "is hidden." % opt_name,
                DiagResult.CONFIDENCE_POSSIBLE, SRC))
    except Exception:
        pass

    return results


# L3 ────────────────────────────────────────────────────────────────────
def diagnose_link_display_settings(host_doc, link_instance, view):
    """Report the link display mode via view.GetLinkOverrides().

    GetLinkOverrides takes an ElementId — we use the RevitLinkInstance Id.
    If that fails we fall back to the RevitLinkType Id.
    This is noted in the API ASSUMPTIONS for manual verification.

    Returns (results, mode_string).
    mode_string is one of "ByHostView", "ByLinkedView", "Custom", "Unknown".
    """
    SRC = "diagnose_link_display_settings"
    results = []
    mode = "Unknown"
    linked_view_id_out = None

    if not hasattr(view, "GetLinkOverrides"):
        results.append(_unknown(
            "LINK_DISPLAY_UNKNOWN", "Link Display Settings",
            "View.GetLinkOverrides is not available in this API version.",
            SRC))
        return results, mode, None

    # If a view template controls Revit Links, we must get overrides from the template.
    effective_view = view
    try:
        tmpl_id = view.ViewTemplateId
        if tmpl_id and tmpl_id != ElementId.InvalidElementId:
            tmpl = host_doc.GetElement(tmpl_id)
            if tmpl:
                ctrl_ids = set(tmpl.GetTemplateParameterIds())
                if hasattr(BuiltInParameter, "VIS_GRAPHICS_RVT_LINKS"):
                    bip = BuiltInParameter.VIS_GRAPHICS_RVT_LINKS
                    if ElementId(bip) in ctrl_ids:
                        effective_view = tmpl
    except Exception:
        pass

    # Try with RevitLinkInstance Id first, then RevitLinkType Id
    settings = None
    used_id_label = None
    for try_id, label in [
        (link_instance.Id, "instance Id"),
        (link_instance.GetTypeId(), "type Id"),
    ]:
        try:
            settings = effective_view.GetLinkOverrides(try_id)
            if settings is not None:
                used_id_label = label
                break
        except Exception:
            continue

    if settings is None:
        results.append(_unknown(
            "LINK_DISPLAY_UNKNOWN", "Link Display Settings",
            "Unable to retrieve link overrides "
            "(tried instance Id and type Id).", SRC))
        return results, mode, None

    # Read the mode
    # RevitLinkGraphicsSettings has LinkedViewId and a
    # GetLinkVisibilityType() or similar member.
    try:
        # Try GetLinkVisibilityType()  (returns LinkVisibility enum)
        if hasattr(settings, "GetLinkVisibilityType"):
            vis_type = settings.GetLinkVisibilityType()
            vis_name = str(vis_type).split(".")[-1]

            if hasattr(LinkVisibility, "ByHostView"):
                if vis_type == LinkVisibility.ByHostView:
                    mode = "ByHostView"
                elif vis_type == LinkVisibility.ByLinkedView:
                    mode = "ByLinkedView"
                elif vis_type == LinkVisibility.Custom:
                    mode = "Custom"
                else:
                    mode = vis_name
            else:
                mode = vis_name
        else:
            # Fallback: check LinkedViewId
            if hasattr(settings, "LinkedViewId"):
                lv_id = settings.LinkedViewId
                if lv_id and lv_id != ElementId.InvalidElementId:
                    mode = "ByLinkedView"
                else:
                    mode = "ByHostView"
    except Exception as ex:
        results.append(_unknown(
            "LINK_DISPLAY_UNKNOWN", "Link Display Mode",
            "Unable to determine display mode: %s" % str(ex), SRC))
        return results, mode, None

    # LinkedViewId for ByLinkedView mode
    try:
        if hasattr(settings, "LinkedViewId"):
            lv_id = settings.LinkedViewId
            if lv_id and lv_id != ElementId.InvalidElementId:
                linked_view_id_out = lv_id
    except Exception:
        pass

    if mode == "ByHostView":
        results.append(_pass(
            "LINK_DISPLAY_BY_HOST", "Display: By Host View",
            "Link is displayed using the host view's V/G settings "
            "(retrieved via %s)." % used_id_label, SRC))
    elif mode == "ByLinkedView":
        lv_name = ""
        if linked_view_id_out:
            try:
                link_doc = link_instance.GetLinkDocument()
                if link_doc:
                    lv = link_doc.GetElement(linked_view_id_out)
                    lv_name = lv.Name if lv else str(linked_view_id_out.IntegerValue)
            except Exception:
                lv_name = str(linked_view_id_out.IntegerValue)
        results.append(_pass(
            "LINK_DISPLAY_BY_LINKED", "Display: By Linked View",
            "Link is displayed using linked view '%s' "
            "(retrieved via %s)." % (lv_name, used_id_label), SRC))
    elif mode == "Custom":
        results.append(_warning(
            "LINK_DISPLAY_CUSTOM", "Display: Custom Settings",
            "Link uses custom display settings.  Custom settings are "
            "not fully inspectable via the Revit 2024 API; some checks "
            "below may be UNKNOWN.",
            DiagResult.CONFIDENCE_POSSIBLE, SRC))
    else:
        results.append(_unknown(
            "LINK_DISPLAY_UNKNOWN", "Link Display Mode",
            "Display mode is '%s' — unable to interpret." % mode, SRC))

    return results, mode, linked_view_id_out


# L4 ────────────────────────────────────────────────────────────────────
def _map_linked_cat_to_host(host_doc, linked_element):
    """Map a linked element's category to the host BuiltInCategory.

    We get the linked category's BuiltInCategory enum value and look it
    up in the host document's category table.  Returns (host_cat_id, name)
    or (None, name).
    """
    cat = linked_element.Category
    if cat is None:
        return None, None
    cat_name = cat.Name

    try:
        bic = None
        # Category.Id.IntegerValue for BuiltInCategory is the negative enum
        cat_int = cat.Id.IntegerValue
        # In Revit, BuiltInCategory enum values are negative integers
        # matching Category.Id.IntegerValue for built-in categories.
        if cat_int < 0:
            bic = cat_int  # this IS the BuiltInCategory int value
            host_cat_id = ElementId(bic)
            return host_cat_id, cat_name
    except Exception:
        pass
    return None, cat_name


def diagnose_link_element_in_context(
    host_doc, link_doc, link_instance, linked_element,
    host_view, display_mode, linked_view_id
):
    """Element-level checks, branched by display mode.

    - ByHostView:  map category to host, check host V/G.
    - ByLinkedView: re-run Pass 1 engine on linked doc + linked view.
    - Custom:      partial checks, rest UNKNOWN.
    """
    SRC = "diagnose_link_element_in_context"
    results = []

    if display_mode == "ByLinkedView":
        return _check_by_linked_view(
            host_doc, link_doc, link_instance, linked_element,
            host_view, linked_view_id)

    if display_mode == "ByHostView":
        return _check_by_host_view(
            host_doc, link_doc, link_instance, linked_element, host_view)

    # Custom or Unknown — partial
    return _check_custom_mode(
        host_doc, link_doc, linked_element, host_view)


def _check_by_host_view(host_doc, link_doc, link_instance, linked_element, host_view):
    """ByHostView: use host view V/G for linked element's category."""
    SRC = "diagnose_link_element_by_host"
    results = []

    # --- Category visibility (mapped to host) ---
    host_cat_id, cat_name = _map_linked_cat_to_host(host_doc, linked_element)
    if host_cat_id is not None:
        try:
            if host_view.GetCategoryHidden(host_cat_id):
                results.append(_error(
                    "CAT_HIDDEN",
                    "Category Hidden (Host V/G)",
                    "Category '%s' (mapped from linked model) is hidden "
                    "in the host view's Visibility/Graphics." % cat_name,
                    DiagResult.CONFIDENCE_CONFIRMED, SRC))
            else:
                results.append(_pass(
                    "CAT_VISIBLE",
                    "Category Visible (Host V/G)",
                    "Category '%s' is visible in the host view." % cat_name,
                    SRC))
        except Exception as ex:
            results.append(_unknown(
                "CAT_UNKNOWN", "Category Visibility (Host)",
                "Unable to check '%s' in host view: %s" % (cat_name, str(ex)),
                SRC))
    elif cat_name:
        results.append(_unknown(
            "CAT_UNKNOWN", "Category Mapping",
            "Category '%s' could not be mapped to a host built-in "
            "category." % cat_name, SRC))

    # --- Host view template control ---
    tmpl_res, tmpl_info = diagnose_view_template(host_doc, linked_element, host_view)
    # Only keep the template-applied info, not element-specific params
    for r in tmpl_res:
        r.source = SRC + " (host template)"
    results.extend(tmpl_res)
    if tmpl_info:
        _attribute_template(results, tmpl_info)

    # --- Host filters MAY apply to linked elements ---
    try:
        filter_ids = host_view.GetFilters()
        if filter_ids and len(filter_ids) > 0:
            # We cannot reliably test whether host filters match a linked
            # element because the element belongs to a different Document.
            # Report POSSIBLE for any hiding filter whose categories include
            # the element's mapped category.
            for fid in filter_ids:
                try:
                    fe = host_doc.GetElement(fid)
                    if fe is None:
                        continue
                    f_name = getattr(fe, "Name", None) or str(fid.IntegerValue)

                    is_enabled = True
                    try:
                        if hasattr(host_view, "GetIsFilterEnabled"):
                            is_enabled = host_view.GetIsFilterEnabled(fid)
                    except Exception:
                        pass
                    if not is_enabled:
                        continue

                    is_visible = True
                    try:
                        is_visible = host_view.GetFilterVisibility(fid)
                    except Exception:
                        pass
                    if is_visible:
                        continue

                    # Filter is enabled and hides — check category
                    if host_cat_id and hasattr(fe, "GetCategories"):
                        try:
                            f_cats = fe.GetCategories()
                            if host_cat_id in f_cats:
                                results.append(_warning(
                                    "FILTER_POSSIBLE",
                                    "Host Filter May Apply: '%s'" % f_name,
                                    "Host view filter '%s' hides elements of "
                                    "category '%s'.  It may apply to this "
                                    "linked element but cannot be confirmed "
                                    "across documents." % (f_name, cat_name),
                                    DiagResult.CONFIDENCE_POSSIBLE, SRC))
                        except Exception:
                            pass
                except Exception:
                    continue
    except Exception:
        pass

    # --- Linked workset / phase: UNKNOWN (cross-doc) ---
    results.append(_unknown(
        "LINK_ELEM_WORKSET_UNKNOWN",
        "Linked Element Workset (By Host View)",
        "Workset visibility for elements inside a linked model cannot be "
        "reliably determined from the host view in By Host View mode.", SRC))

    results.append(_unknown(
        "LINK_ELEM_PHASE_UNKNOWN",
        "Linked Element Phasing (By Host View)",
        "Phase mapping between the linked document and the host view "
        "cannot be reliably determined.  Do not compare phase names "
        "across documents.", SRC))

    # --- Geometry and Masking (Host View vs Linked Element) ---
    try:
        link_transform = link_instance.GetTotalTransform()
        results.extend(diagnose_geometry_visibility(host_doc, linked_element, host_view, link_transform))
        results.extend(diagnose_masking_occlusion(host_doc, linked_element, host_view, link_transform))
    except Exception as ex:
        results.append(_unknown(
            "LINK_GEOM_UNKNOWN",
            "Linked Geometry Checks",
            "Unable to run geometric or masking checks for the linked element: %s" % str(ex), SRC))

    return results


def _check_by_linked_view(
    host_doc, link_doc, link_instance, linked_element,
    host_view, linked_view_id
):
    """ByLinkedView: re-run Pass 1 checks against the linked view
    using only linked-document objects."""
    SRC = "diagnose_link_element_by_linked_view"
    results = []

    if linked_view_id is None or linked_view_id == ElementId.InvalidElementId:
        results.append(_unknown(
            "LINK_LINKED_VIEW_UNKNOWN",
            "Linked View Not Identified",
            "Display mode is By Linked View but the linked view ID "
            "could not be determined.", SRC))
        return results

    linked_view = link_doc.GetElement(linked_view_id)
    if linked_view is None:
        results.append(_unknown(
            "LINK_LINKED_VIEW_UNKNOWN",
            "Linked View Not Found",
            "Linked view ID %s could not be found in the linked document."
            % str(linked_view_id.IntegerValue), SRC))
        return results

    lv_name = linked_view.Name or str(linked_view_id.IntegerValue)

    results.append(_pass(
        "LINK_USING_LINKED_VIEW", "Using Linked View",
        "Evaluating element visibility against linked view '%s'." % lv_name,
        SRC))

    # Re-run Pass 1 checks using LINKED doc + LINKED view
    # (element hidden, category, template, workset, filters, phase,
    #  design option, temp hide/isolate)
    prefix = u"Linked view '%s': " % lv_name

    sub_results = []
    sub_results.extend(
        diagnose_element_hidden(link_doc, linked_element, linked_view))
    sub_results.extend(
        diagnose_category_visibility(link_doc, linked_element, linked_view))

    tmpl_res, tmpl_info = diagnose_view_template(
        link_doc, linked_element, linked_view)
    sub_results.extend(tmpl_res)

    sub_results.extend(
        diagnose_workset_visibility(link_doc, linked_element, linked_view))
    sub_results.extend(
        diagnose_filters(link_doc, linked_element, linked_view))
    sub_results.extend(
        diagnose_phasing(link_doc, linked_element, linked_view))
    sub_results.extend(
        diagnose_design_option(link_doc, linked_element, linked_view))
    sub_results.extend(
        diagnose_geometry_visibility(link_doc, linked_element, linked_view))
    sub_results.extend(
        diagnose_masking_occlusion(link_doc, linked_element, linked_view))

    if tmpl_info:
        _attribute_template(sub_results, tmpl_info)

    # Prefix titles so the user knows these are linked-view results
    for r in sub_results:
        r.title = prefix + r.title
        r.source = SRC + " > " + r.source

    results.extend(sub_results)
    return results


def _check_custom_mode(host_doc, link_doc, linked_element, host_view):
    """Custom display: partial inspection, rest UNKNOWN."""
    SRC = "diagnose_link_element_custom"
    results = []

    # Category visibility in host view (still applies in custom)
    host_cat_id, cat_name = _map_linked_cat_to_host(host_doc, linked_element)
    if host_cat_id is not None:
        try:
            if host_view.GetCategoryHidden(host_cat_id):
                results.append(_error(
                    "CAT_HIDDEN",
                    "Category Hidden (Custom Mode)",
                    "Category '%s' is hidden in the host view.  "
                    "Custom mode still respects host category visibility."
                    % cat_name,
                    DiagResult.CONFIDENCE_POSSIBLE, SRC))
            else:
                results.append(_pass(
                    "CAT_VISIBLE",
                    "Category Visible (Custom Mode)",
                    "Category '%s' appears visible in the host view."
                    % cat_name, SRC))
        except Exception:
            pass

    results.append(_unknown(
        "LINK_CUSTOM_UNKNOWN",
        "Custom Display Settings",
        "Custom link display settings are not fully inspectable via the "
        "Revit 2024 API.  Filter, workset, and phase overrides within "
        "the custom configuration cannot be determined.", SRC))

    return results


# ── Link engine orchestrator ──────────────────────────────────────────
def run_link_checks(host_doc, link_instance, element_id, host_view):
    """Run all link checks in order.  Returns (results, linked_element)."""
    all_results = []

    # L1: status + element resolution
    res, link_doc, linked_element = diagnose_link_status(
        host_doc, link_instance, element_id, host_view)
    all_results.extend(res)
    if link_doc is None or linked_element is None:
        return all_results, linked_element

    if isinstance(linked_element, ElementType):
        return all_results, linked_element

    # L2: link instance host-side visibility
    inst_results = diagnose_link_instance_visibility(
        host_doc, link_instance, host_view)
    all_results.extend(inst_results)

    # Check if the link instance itself is blocked
    link_blocked = any(
        r.status == DiagResult.STATUS_BLOCKED
        for r in inst_results
    )

    # L3: display settings
    disp_results, mode, linked_view_id = diagnose_link_display_settings(
        host_doc, link_instance, host_view)
    all_results.extend(disp_results)

    # L4: element checks by display mode
    if link_blocked:
        all_results.append(_warning(
            "LINK_ELEM_NOT_MEANINGFUL",
            "Element Checks Deferred",
            "The link instance itself is hidden (see above).  "
            "Element-level checks are not meaningful until the link "
            "instance is visible.",
            DiagResult.CONFIDENCE_CONFIRMED,
            "run_link_checks"))
    else:
        elem_results = diagnose_link_element_in_context(
            host_doc, link_doc, link_instance, linked_element,
            host_view, mode, linked_view_id)
        all_results.extend(elem_results)

    return all_results, linked_element


# ═══════════════════════════════════════════════════════════════════════
# ENGINE
# ═══════════════════════════════════════════════════════════════════════

def _attribute_template(all_results, template_info):
    """Post-process: if a blocker's V/G group is template-controlled,
    append a note to its message attributing the setting to the template."""
    tname = template_info.get("name", "")
    controlled = template_info.get("controlled_groups", set())
    if not tname or not controlled:
        return

    code_to_groups = {
        "CAT_HIDDEN":  ("model_categories", "annotation_categories",
                        "analytical_categories", "import_categories"),
        "LINK_CAT_HIDDEN": ("revit_links",),
        "WORKSET_HIDDEN": ("worksets",),
        "FILTER_HIDDEN":  ("filters",),
        "FILTER_POSSIBLE": ("filters",),
        "PHASE_DONT_SHOW": ("phase_filter", "phase"),
    }

    for r in all_results:
        groups = code_to_groups.get(r.code, ())
        for g in groups:
            if g in controlled:
                r.message += (
                    u"  This setting is controlled by View Template '%s'."
                    % tname)
                break


def run_all_checks(doc, element_id, view, link_instance=None):
    """Run every diagnostic check in order.  Returns (results, element)."""
    all_results = []

    if link_instance is not None:
        return run_link_checks(doc, link_instance, element_id, view)

    # 1. Existence
    res, element = diagnose_element_exists(doc, element_id, view)
    all_results.extend(res)
    if element is None or isinstance(element, ElementType):
        return all_results, element

    # 2. Explicit hide
    all_results.extend(diagnose_element_hidden(doc, element, view))

    # 3. Category visibility
    all_results.extend(diagnose_category_visibility(doc, element, view))

    # 4. View template (also returns template_info for attribution)
    tmpl_res, tmpl_info = diagnose_view_template(doc, element, view)
    all_results.extend(tmpl_res)

    # 5. Workset visibility
    all_results.extend(diagnose_workset_visibility(doc, element, view))

    # 6. Filters
    all_results.extend(diagnose_filters(doc, element, view))

    # 7. Phasing
    all_results.extend(diagnose_phasing(doc, element, view))

    # 8. Design option
    all_results.extend(diagnose_design_option(doc, element, view))

    # 9. Geometry (view range, crop region, section box) — geometric fallback
    all_results.extend(diagnose_geometry_visibility(doc, element, view))

    # 10. Masking Regions (2D occlusion)
    all_results.extend(diagnose_masking_occlusion(doc, element, view))

    # Post-process: attribute blockers to template
    if tmpl_info:
        _attribute_template(all_results, tmpl_info)

    return all_results, element


def classify_results(results):
    """Split results into display buckets for the UI."""
    confirmed = []
    possible  = []
    unknown   = []
    passes    = []

    for r in results:
        s = r.status
        if s == DiagResult.STATUS_BLOCKED:
            confirmed.append(r)
        elif s == DiagResult.STATUS_POSSIBLE:
            possible.append(r)
        elif s == DiagResult.STATUS_UNKNOWN:
            unknown.append(r)
        else:
            passes.append(r)

    confirmed.sort(key=lambda r: BLOCKER_PRIORITY.get(r.code, 99))

    primary   = confirmed[0] if confirmed else None
    secondary = confirmed[1:] if len(confirmed) > 1 else []

    return {
        "primary":   primary,
        "secondary": secondary,
        "possible":  possible,
        "unknown":   unknown,
        "passes":    passes,
        "all":       results,
    }


# ═══════════════════════════════════════════════════════════════════════
# UI WINDOW
# ═══════════════════════════════════════════════════════════════════════

class VisibilityDoctorWindow(forms.WPFWindow):
    """Main WPF dialog."""

    def __init__(self, uidoc, doc):
        xaml_path = os.path.join(os.path.dirname(__file__), "ui.xaml")
        forms.WPFWindow.__init__(self, xaml_path)

        self._uidoc = uidoc
        self._doc   = doc

        # Pre-create reusable brushes
        self._br_red    = SolidColorBrush(Color.FromRgb(211, 47, 47))
        self._br_orange = SolidColorBrush(Color.FromRgb(245, 124, 0))
        self._br_green  = SolidColorBrush(Color.FromRgb(56, 142, 60))
        self._br_gray   = SolidColorBrush(Color.FromRgb(158, 158, 158))
        self._br_blue   = SolidColorBrush(Color.FromRgb(76, 35, 53))     # Deep Plum
        self._br_text   = SolidColorBrush(Color.FromRgb(59, 51, 46))     # Soft Black
        self._br_sub    = SolidColorBrush(Color.FromRgb(117, 117, 117))
        self._br_white  = SolidColorBrush(Color.FromRgb(255, 255, 255))
        self._br_bg_red    = SolidColorBrush(Color.FromRgb(253, 237, 236))
        self._br_bg_orange = SolidColorBrush(Color.FromRgb(255, 243, 224))
        self._br_bg_green  = SolidColorBrush(Color.FromRgb(232, 245, 233))
        self._br_bg_gray   = SolidColorBrush(Color.FromRgb(245, 245, 245))
        self._br_bg_blue   = SolidColorBrush(Color.FromRgb(245, 240, 232)) # Warm Cream

        # Data stores
        self._view_map = {}       # display_string -> View
        self._all_view_names = [] # sorted list of all display strings
        self._link_map = {}       # display_string -> RevitLinkInstance
        self._all_link_names = [] # sorted list of all link display strings

        # Populate UI
        self._populate_views()
        self._populate_links()

        # Wire events (Python side — more reliable than XAML attributes)
        self.rbLinkedModel.Checked  += self._on_link_radio
        self.rbCurrentModel.Checked += self._on_current_radio
        self.txtViewFilter.TextChanged += self._on_view_filter
        self.txtLinkFilter.TextChanged += self._on_link_filter
        self.btnDiagnose.Click += self._on_diagnose
        self.btnLinkedIn.Click += self._on_linkedin_click

    # ─── View population ──────────────────────────────────────────────
    def _populate_views(self):
        collector = FilteredElementCollector(self._doc).OfClass(View)
        views = []
        for v in collector:
            try:
                if v.IsTemplate:
                    continue
                if v.ViewType not in GRAPHICAL_VIEW_TYPES:
                    continue
                vt_label = VIEW_TYPE_LABELS.get(str(v.ViewType), str(v.ViewType))
                label = "%s:  %s" % (vt_label, v.Name)
                views.append((label, v))
            except Exception:
                continue

        views.sort(key=lambda t: t[0])

        # Determine active view for default selection
        active = None
        try:
            active = self._uidoc.ActiveGraphicalView
        except Exception:
            try:
                active = self._uidoc.ActiveView
            except Exception:
                pass

        sel_idx = 0
        for i, (label, v) in enumerate(views):
            self._view_map[label] = v
            self._all_view_names.append(label)
            self.cmbView.Items.Add(label)
            if active and v.Id == active.Id:
                sel_idx = i

        if self.cmbView.Items.Count > 0:
            self.cmbView.SelectedIndex = sel_idx

    def _populate_links(self):
        try:
            collector = (FilteredElementCollector(self._doc)
                         .OfClass(RevitLinkInstance))
            for li in collector:
                try:
                    li_name = li.Name or ""
                    li_id = li.Id.IntegerValue
                    status_tag = ""
                    try:
                        ld = li.GetLinkDocument()
                        if ld is None:
                            status_tag = " (unloaded)"
                    except Exception:
                        status_tag = " (unloaded)"
                    label = "%s  [ID: %d]%s" % (li_name, li_id, status_tag)
                    self._link_map[label] = li
                    self._all_link_names.append(label)
                    self.cmbLinkedModel.Items.Add(label)
                except Exception:
                    continue
            if self.cmbLinkedModel.Items.Count > 0:
                self.cmbLinkedModel.SelectedIndex = 0
        except Exception:
            pass

    # ─── Event handlers ───────────────────────────────────────────────
    def _on_link_radio(self, sender, args):
        self.pnlLinkSelect.Visibility = WinVisibility.Visible

    def _on_current_radio(self, sender, args):
        self.pnlLinkSelect.Visibility = WinVisibility.Collapsed

    def _on_link_filter(self, sender, args):
        filt = (self.txtLinkFilter.Text or "").strip().lower()
        prev = self.cmbLinkedModel.SelectedItem
        self.cmbLinkedModel.Items.Clear()
        match_idx = -1
        for name in self._all_link_names:
            if not filt or filt in name.lower():
                self.cmbLinkedModel.Items.Add(name)
                if name == prev:
                    match_idx = self.cmbLinkedModel.Items.Count - 1
        if match_idx >= 0:
            self.cmbLinkedModel.SelectedIndex = match_idx
        elif self.cmbLinkedModel.Items.Count > 0:
            self.cmbLinkedModel.SelectedIndex = 0

    def _on_view_filter(self, sender, args):
        filt = (self.txtViewFilter.Text or "").strip().lower()
        prev = self.cmbView.SelectedItem
        self.cmbView.Items.Clear()
        match_idx = -1
        for name in self._all_view_names:
            if not filt or filt in name.lower():
                self.cmbView.Items.Add(name)
                if name == prev:
                    match_idx = self.cmbView.Items.Count - 1
        if match_idx >= 0:
            self.cmbView.SelectedIndex = match_idx
        elif self.cmbView.Items.Count > 0:
            self.cmbView.SelectedIndex = 0

    def _on_linkedin_click(self, sender, args):
        import webbrowser
        try:
            webbrowser.open("https://www.linkedin.com/in/hazem-sakr/")
        except Exception:
            pass

    def _on_diagnose(self, sender, args):
        try:
            self._run_diagnosis()
        except Exception as ex:
            self._show_error_result("Unexpected error: %s" % str(ex))

    # ─── Core diagnosis entry point ───────────────────────────────────
    def _run_diagnosis(self):
        self.pnlResults.Children.Clear()
        self.borderResults.Visibility = WinVisibility.Visible

        # Validate element ID
        raw_text = (self.txtElementId.Text or "").strip()
        if not raw_text:
            self._show_error_result("Please enter an Element ID.")
            return
        ok, int_val = Int64.TryParse(raw_text)
        if not ok:
            self._show_error_result(
                "'%s' is not a valid Element ID.  "
                "Enter a numeric ID (e.g. 123456)." % raw_text)
            return
        eid = ElementId(int_val)

        # Validate view selection
        view_label = self.cmbView.SelectedItem
        if not view_label or view_label not in self._view_map:
            self._show_error_result("Please select a view.")
            return
        view = self._view_map[view_label]

        # Link mode?
        is_link = (self.rbLinkedModel.IsChecked is True
                   or self.rbLinkedModel.IsChecked == True)
        
        link_instance = None
        if is_link:
            link_label = self.cmbLinkedModel.SelectedItem
            if not link_label or link_label not in self._link_map:
                self._show_error_result("Please select a linked model.")
                return
            link_instance = self._link_map[link_label]

        # Run engine
        all_results, element = run_all_checks(
            self._doc, eid, view, link_instance=link_instance)

        classified = classify_results(all_results)

        # ── Build results UI ──────────────────────────────────────────
        # Build link info string if applicable
        link_str = None
        if link_instance:
            li_name = getattr(link_instance, "Name", "") or str(link_instance.Id.IntegerValue)
            link_str = u"%s (ID: %s)" % (li_name, str(link_instance.Id.IntegerValue))

        # Element summary
        if element and not isinstance(element, ElementType):
            # For linked elements, the element belongs to the linked doc.
            # We must pass the correct document to get_element_info.
            elem_doc = link_instance.GetLinkDocument() if link_instance else self._doc
            info = get_element_info(elem_doc, element)
            if link_str:
                info["link_instance"] = link_str
            self._add_element_summary(info, view.Name)
        else:
            if link_str:
                self._add_view_label(view.Name + u"  [Link: %s]" % link_str)
            else:
                self._add_view_label(view.Name)

        # Primary diagnosis
        primary = classified["primary"]
        if primary:
            self._add_section_card(
                u"PRIMARY DIAGNOSIS", primary,
                self._br_bg_red, self._br_red)

        # Secondary confirmed blockers
        for sec in classified["secondary"]:
            self._add_section_card(
                u"ADDITIONAL BLOCKER", sec,
                self._br_bg_red, self._br_red)

        # Possible issues
        for pos in classified["possible"]:
            self._add_section_card(
                u"POSSIBLE ISSUE", pos,
                self._br_bg_orange, self._br_orange)

        # Unknown items (skip cross-document API limitations to reduce clutter for visible elements)
        for unk in classified["unknown"]:
            if unk.code in ("LINK_ELEM_WORKSET_UNKNOWN", "LINK_ELEM_PHASE_UNKNOWN"):
                continue
            self._add_section_card(
                u"UNABLE TO DETERMINE", unk,
                self._br_bg_gray, self._br_gray)

        # All-clear vs Inconclusive
        if not primary and not classified["secondary"]:
            inconclusive = classified["possible"] + classified["unknown"]
            # Filter out expected cross-document API limitations
            real_inconclusive = [r for r in inconclusive if r.code not in ("LINK_ELEM_WORKSET_UNKNOWN", "LINK_ELEM_PHASE_UNKNOWN")]
            
            if not real_inconclusive:
                self._add_all_clear()
            else:
                self._add_inconclusive_warning(len(real_inconclusive))

        # Full check list
        self._add_check_list(all_results)

    # ─── Result-building helpers ──────────────────────────────────────
    def _show_error_result(self, msg):
        self.pnlResults.Children.Clear()
        self.borderResults.Visibility = WinVisibility.Visible
        tb = TextBlock()
        tb.Text = msg
        tb.Foreground = self._br_red
        tb.FontSize = 13
        tb.TextWrapping = TextWrapping.Wrap
        tb.Margin = Thickness(0, 4, 0, 4)
        self.pnlResults.Children.Add(tb)

    def _add_view_label(self, view_name):
        tb = TextBlock()
        tb.Text = u"View:  %s" % view_name
        tb.FontSize = 12
        tb.Foreground = self._br_sub
        tb.Margin = Thickness(0, 0, 0, 8)
        self.pnlResults.Children.Add(tb)

    def _add_element_summary(self, info, view_name):
        border = Border()
        border.Background = self._br_bg_blue
        border.BorderBrush = SolidColorBrush(Color.FromRgb(187, 222, 251))
        border.BorderThickness = Thickness(1)
        border.CornerRadius = CornerRadius(4)
        border.Padding = Thickness(12)
        border.Margin = Thickness(0, 0, 0, 10)

        sp = StackPanel()

        # Title
        hdr = TextBlock()
        hdr.Text = u"ELEMENT SUMMARY"
        hdr.FontWeight = FontWeights.Bold
        hdr.FontSize = 12
        hdr.Foreground = self._br_blue
        hdr.Margin = Thickness(0, 0, 0, 6)
        sp.Children.Add(hdr)

        fields = [
            ("Link Instance", info.get("link_instance")),
            ("ID", info.get("id", "?")),
            ("Category", info.get("category")),
            ("Family", info.get("family")),
            ("Type", info.get("type")),
            ("Workset", info.get("workset")),
            ("Design Option", info.get("design_option")),
            ("Phase Created", info.get("phase_created")),
            ("Phase Demolished", info.get("phase_demolished")),
        ]
        for label, val in fields:
            if val is None:
                continue
            row = StackPanel()
            row.Orientation = Orientation.Horizontal
            row.Margin = Thickness(0, 1, 0, 1)
            lbl = TextBlock()
            lbl.Text = u"%s:  " % label
            lbl.FontSize = 12
            lbl.Foreground = self._br_sub
            lbl.Width = 120
            row.Children.Add(lbl)
            vtb = TextBlock()
            vtb.Text = val
            vtb.FontSize = 12
            vtb.Foreground = self._br_text
            vtb.TextWrapping = TextWrapping.Wrap
            row.Children.Add(vtb)
            sp.Children.Add(row)

        # View line
        vrow = StackPanel()
        vrow.Orientation = Orientation.Horizontal
        vrow.Margin = Thickness(0, 1, 0, 0)
        vlbl = TextBlock()
        vlbl.Text = u"View:  "
        vlbl.FontSize = 12
        vlbl.Foreground = self._br_sub
        vlbl.Width = 120
        vrow.Children.Add(vlbl)
        vvtb = TextBlock()
        vvtb.Text = view_name
        vvtb.FontSize = 12
        vvtb.Foreground = self._br_text
        vvtb.TextWrapping = TextWrapping.Wrap
        vrow.Children.Add(vvtb)
        sp.Children.Add(vrow)

        border.Child = sp
        self.pnlResults.Children.Add(border)

    def _add_section_card(self, header_text, result, bg_brush, accent_brush):
        """Add a coloured card for a primary/secondary/possible/unknown result."""
        border = Border()
        border.Background = bg_brush
        border.BorderBrush = accent_brush
        border.BorderThickness = Thickness(0, 0, 0, 3)
        border.CornerRadius = CornerRadius(4)
        border.Padding = Thickness(12)
        border.Margin = Thickness(0, 0, 0, 8)

        sp = StackPanel()

        # Header line with icon
        hdr_panel = StackPanel()
        hdr_panel.Orientation = Orientation.Horizontal

        icon = TextBlock()
        status = result.status
        if status == DiagResult.STATUS_BLOCKED:
            icon.Text = u"\u2716"   # ✖
            icon.Foreground = self._br_red
        elif status == DiagResult.STATUS_POSSIBLE:
            icon.Text = u"\u26A0"   # ⚠
            icon.Foreground = self._br_orange
        elif status == DiagResult.STATUS_UNKNOWN:
            icon.Text = u"?"
            icon.Foreground = self._br_gray
        else:
            icon.Text = u"\u2714"   # ✔
            icon.Foreground = self._br_green
        icon.FontSize = 14
        icon.Margin = Thickness(0, 0, 6, 0)
        icon.VerticalAlignment = VerticalAlignment.Center
        hdr_panel.Children.Add(icon)

        hdr = TextBlock()
        hdr.Text = header_text
        hdr.FontWeight = FontWeights.Bold
        hdr.FontSize = 12
        hdr.Foreground = self._br_text
        hdr.VerticalAlignment = VerticalAlignment.Center
        hdr_panel.Children.Add(hdr)

        sp.Children.Add(hdr_panel)

        # Title
        ttl = TextBlock()
        ttl.Text = result.title
        ttl.FontWeight = FontWeights.SemiBold
        ttl.FontSize = 12
        ttl.Foreground = accent_brush
        ttl.Margin = Thickness(20, 4, 0, 0)
        sp.Children.Add(ttl)

        # Message
        msg = TextBlock()
        msg.Text = result.message
        msg.TextWrapping = TextWrapping.Wrap
        msg.FontSize = 11
        msg.Foreground = self._br_text
        msg.Margin = Thickness(20, 2, 0, 0)
        sp.Children.Add(msg)

        border.Child = sp
        self.pnlResults.Children.Add(border)

    def _add_inconclusive_warning(self, count):
        border = Border()
        border.Background = self._br_bg_orange
        border.BorderBrush = self._br_orange
        border.BorderThickness = Thickness(0, 0, 0, 3)
        border.CornerRadius = CornerRadius(4)
        border.Padding = Thickness(12)
        border.Margin = Thickness(0, 0, 0, 8)

        sp = StackPanel()

        hdr_panel = StackPanel()
        hdr_panel.Orientation = Orientation.Horizontal
        icon = TextBlock()
        icon.Text = u"\u26A0"
        icon.Foreground = self._br_orange
        icon.FontSize = 16
        icon.Margin = Thickness(0, 0, 6, 0)
        icon.VerticalAlignment = VerticalAlignment.Center
        hdr_panel.Children.Add(icon)
        hdr = TextBlock()
        hdr.Text = u"NO CONFIRMED BLOCKERS"
        hdr.FontWeight = FontWeights.Bold
        hdr.FontSize = 13
        hdr.Foreground = self._br_orange
        hdr.VerticalAlignment = VerticalAlignment.Center
        hdr_panel.Children.Add(hdr)
        sp.Children.Add(hdr_panel)

        msg = TextBlock()
        msg.Text = (u"No explicit visibility settings were confirmed to hide "
                     u"this element. However, %d check(s) were inconclusive "
                     u"(see POSSIBLE ISSUE or UNABLE TO DETERMINE above)." % count)
        msg.TextWrapping = TextWrapping.Wrap
        msg.FontSize = 11
        msg.Foreground = self._br_text
        msg.Margin = Thickness(22, 4, 0, 0)
        sp.Children.Add(msg)

        border.Child = sp
        self.pnlResults.Children.Add(border)

    def _add_all_clear(self):
        border = Border()
        border.Background = self._br_bg_green
        border.BorderBrush = self._br_green
        border.BorderThickness = Thickness(0, 0, 0, 3)
        border.CornerRadius = CornerRadius(4)
        border.Padding = Thickness(12)
        border.Margin = Thickness(0, 0, 0, 8)

        sp = StackPanel()

        hdr_panel = StackPanel()
        hdr_panel.Orientation = Orientation.Horizontal
        icon = TextBlock()
        icon.Text = u"\u2714"
        icon.Foreground = self._br_green
        icon.FontSize = 16
        icon.Margin = Thickness(0, 0, 6, 0)
        icon.VerticalAlignment = VerticalAlignment.Center
        hdr_panel.Children.Add(icon)
        hdr = TextBlock()
        hdr.Text = u"NO VISIBILITY BLOCKERS FOUND"
        hdr.FontWeight = FontWeights.Bold
        hdr.FontSize = 13
        hdr.Foreground = self._br_green
        hdr.VerticalAlignment = VerticalAlignment.Center
        hdr_panel.Children.Add(hdr)
        sp.Children.Add(hdr_panel)

        msg = TextBlock()
        msg.Text = (u"No explicit visibility settings were found hiding "
                     u"this element. All checks conclusively passed. If the "
                     u"element is still not visible, the cause may be geometric "
                     u"(view range, crop region, section box).")
        msg.TextWrapping = TextWrapping.Wrap
        msg.FontSize = 11
        msg.Foreground = self._br_text
        msg.Margin = Thickness(22, 4, 0, 0)
        sp.Children.Add(msg)

        border.Child = sp
        self.pnlResults.Children.Add(border)

    def _add_check_list(self, all_results):
        """Add the detailed ALL CHECKS section."""
        border = Border()
        border.Background = self._br_white
        border.BorderBrush = SolidColorBrush(Color.FromRgb(224, 224, 224))
        border.BorderThickness = Thickness(1)
        border.CornerRadius = CornerRadius(4)
        border.Padding = Thickness(12)
        border.Margin = Thickness(0, 4, 0, 0)

        sp = StackPanel()
        hdr = TextBlock()
        hdr.Text = u"ALL CHECKS"
        hdr.FontWeight = FontWeights.Bold
        hdr.FontSize = 12
        hdr.Foreground = self._br_blue
        hdr.Margin = Thickness(0, 0, 0, 8)
        sp.Children.Add(hdr)

        for r in all_results:
            row = StackPanel()
            row.Margin = Thickness(0, 3, 0, 3)

            # Icon + title line
            line = StackPanel()
            line.Orientation = Orientation.Horizontal

            icon = TextBlock()
            icon.FontSize = 12
            icon.Margin = Thickness(0, 0, 6, 0)
            icon.VerticalAlignment = VerticalAlignment.Center
            s = r.status
            if s == DiagResult.STATUS_BLOCKED:
                icon.Text = u"\u2716"
                icon.Foreground = self._br_red
            elif s == DiagResult.STATUS_POSSIBLE:
                icon.Text = u"\u26A0"
                icon.Foreground = self._br_orange
            elif s == DiagResult.STATUS_UNKNOWN:
                icon.Text = u"?"
                icon.Foreground = self._br_gray
            else:
                icon.Text = u"\u2714"
                icon.Foreground = self._br_green
            line.Children.Add(icon)

            ttl = TextBlock()
            ttl.Text = r.title
            ttl.FontWeight = FontWeights.SemiBold
            ttl.FontSize = 12
            ttl.Foreground = self._br_text
            line.Children.Add(ttl)

            badge = TextBlock()
            badge.Text = u"  [%s]" % s
            badge.FontSize = 10
            badge.VerticalAlignment = VerticalAlignment.Center
            if s == DiagResult.STATUS_BLOCKED:
                badge.Foreground = self._br_red
            elif s == DiagResult.STATUS_POSSIBLE:
                badge.Foreground = self._br_orange
            elif s == DiagResult.STATUS_UNKNOWN:
                badge.Foreground = self._br_gray
            else:
                badge.Foreground = self._br_green
            line.Children.Add(badge)

            row.Children.Add(line)

            # Message
            msg = TextBlock()
            msg.Text = r.message
            msg.TextWrapping = TextWrapping.Wrap
            msg.FontSize = 11
            msg.Foreground = self._br_sub
            msg.Margin = Thickness(18, 1, 0, 0)
            row.Children.Add(msg)

            sp.Children.Add(row)

        border.Child = sp
        self.pnlResults.Children.Add(border)


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        _uidoc = __revit__.ActiveUIDocument        # noqa: F821
        _doc   = _uidoc.Document
    except Exception:
        from pyrevit import revit
        _uidoc = revit.uidoc
        _doc   = revit.doc

    if _uidoc is None or _doc is None:
        forms.alert("No document is open.", title="Visibility Doctor")
    else:
        dlg = VisibilityDoctorWindow(_uidoc, _doc)
        dlg.ShowDialog()
