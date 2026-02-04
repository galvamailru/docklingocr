import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TesseractCliOcrOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import PictureItem, TableItem
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Docling PDF OCR API (OCR2)", version="2.0.0")

# Serve simple UI
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)


def _bbox_to_list(bbox: Any) -> Optional[List[float]]:
    """Safely normalize bbox to list [left, top, right, bottom] or [x0, y0, x1, y1]."""
    if bbox is None:
        return None
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        return [float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])]
    if isinstance(bbox, dict):
        for keys in (("left", "top", "right", "bottom"), ("l", "t", "r", "b"), ("x0", "y0", "x1", "y1")):
            if all(k in bbox for k in keys):
                try:
                    return [float(bbox[k]) for k in keys]
                except (TypeError, ValueError):
                    pass

    # Try attribute names used by Docling / docling_core BoundingBox
    for attrs in (
        ("left", "top", "right", "bottom"),
        ("l", "t", "r", "b"),
        ("x0", "y0", "x1", "y1"),
    ):
        coords: List[float] = []
        for attr in attrs:
            if hasattr(bbox, attr):
                try:
                    coords.append(float(getattr(bbox, attr)))
                except (TypeError, ValueError):
                    break
        if len(coords) == 4:
            return coords

    # Fallback: any object with 4 numeric fields (e.g. dataclass)
    if hasattr(bbox, "__iter__") and not isinstance(bbox, (str, bytes)):
        try:
            vals = list(bbox)[:4]
            if len(vals) == 4:
                return [float(v) for v in vals]
        except (TypeError, ValueError):
            pass
    return None


def _extract_page_and_bbox(element: Any) -> tuple[Optional[int], Optional[List[float]]]:
    """
    Docling хранит координаты/страницу в provenance: element.prov[0].bbox, element.prov[0].page_no.
    Проверяем все prov и варианты имён полей (bbox, bounding_box, box).
    """
    prov = getattr(element, "prov", None)
    if isinstance(prov, list) and prov:
        for p in prov:
            page_no = getattr(p, "page_no", None) or getattr(p, "page", None)
            bbox = (
                getattr(p, "bbox", None)
                or getattr(p, "bounding_box", None)
                or getattr(p, "box", None)
            )
            bbox_list = _bbox_to_list(bbox)
            if bbox_list is not None or page_no is not None:
                return (int(page_no) if page_no is not None else None, bbox_list)

    # fallback: bbox на самом элементе
    for attr in ("bbox", "bounding_box", "box"):
        bbox_list = _bbox_to_list(getattr(element, attr, None))
        if bbox_list is not None:
            return (None, bbox_list)
    return (None, None)


def _iterate_items(doc: Any) -> Iterable[Tuple[Any, int]]:
    """Compatible wrapper around Docling's iterate_items()."""
    if hasattr(doc, "iterate_items"):
        yield from doc.iterate_items()
    else:
        # fallback: no structure information
        for el in getattr(doc, "elements", []):
            yield el, 0


def _extract_objects(doc: Any) -> List[Dict[str, Any]]:
    """
    Extract objects from Docling document:
    - tables (TableItem)
    - images (PictureItem)
    - text blocks (everything остальное с текстом)
    """
    objects: List[Dict[str, Any]] = []

    for element, _level in _iterate_items(doc):
        page_no, bbox = _extract_page_and_bbox(element)

        if isinstance(element, TableItem):
            kind = "table"
            # Try to get some textual representation of the table
            text = ""
            export_md = getattr(element, "export_to_markdown", None)
            if callable(export_md):
                try:
                    text = export_md(doc=doc)
                except Exception:  # noqa: BLE001
                    text = ""
        elif isinstance(element, PictureItem):
            kind = "image"
            text = getattr(element, "caption", "") or ""
        else:
            # Treat as text block
            kind = getattr(element, "category", None) or "text"
            text = getattr(element, "text", None) or getattr(element, "content", None) or ""

        objects.append({"type": str(kind), "page": page_no, "bbox": bbox, "text": text})

    # Fallback to exported dict if list is still empty
    if not objects and hasattr(doc, "export_to_dict"):
        data = doc.export_to_dict()
        blocks = data.get("blocks") or data.get("elements") or []
        for block in blocks:
            objects.append(
                {
                    "type": block.get("category") or block.get("type") or "unknown",
                    "page": block.get("page_no") or block.get("page"),
                    "bbox": block.get("bbox"),
                    "text": block.get("text", ""),
                }
            )

    return objects


def convert_pdf(pdf_path: Path) -> Dict[str, Any]:
    """
    Convert a PDF to text and structured objects using Docling.
    OCR uses Russian + English explicitly for correct Cyrillic recognition.
    """
    ocr_options = TesseractCliOcrOptions(lang=["rus", "eng"])
    pipeline_options = PdfPipelineOptions(do_ocr=True, ocr_options=ocr_options)
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        }
    )
    result = converter.convert(pdf_path)
    doc = result.document
    text = doc.export_to_text()
    objects = _extract_objects(doc)
    return {"text": text, "objects": objects}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def index():
    # redirect to static UI
    return JSONResponse(
        status_code=307,
        headers={"Location": "/static/index.html"},
        content={},
    )


@app.post("/parse")
async def parse_pdf(file: UploadFile = File(...)):
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        return JSONResponse(
            status_code=400,
            content={"error": "File must be a PDF"},
        )

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = Path(tmp.name)

        result = convert_pdf(tmp_path)
        return {"filename": file.filename, **result}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to process PDF: {exc}"},
        )
    finally:
        if "tmp_path" in locals() and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

