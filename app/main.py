import base64
import io
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from docling.datamodel.base_models import InputFormat
from pdf2image import convert_from_path
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


def _picture_to_base64(element: PictureItem, doc: Any) -> Optional[str]:
    """Получить изображение PictureItem как PNG в base64 (вырезанный по bbox сегмент)."""
    get_image = getattr(element, "get_image", None)
    if not callable(get_image):
        return None
    try:
        # get_image(document) возвращает PIL Image
        img = get_image(doc)
        if img is None:
            return None
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001
        return None


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

        # Координаты bbox с точностью до одного знака после запятой
        bbox_rounded: Optional[List[float]] = None
        if bbox and len(bbox) >= 4:
            bbox_rounded = [round(float(x), 1) for x in bbox[:4]]

        obj: Dict[str, Any] = {"type": str(kind), "page": page_no, "bbox": bbox_rounded, "text": text}

        # Для изображений — вырезанный по bbox сегмент в base64
        if isinstance(element, PictureItem):
            image_base64 = _picture_to_base64(element, doc)
            if image_base64:
                obj["image_base64"] = image_base64

        objects.append(obj)

    # Fallback to exported dict if list is still empty
    if not objects and hasattr(doc, "export_to_dict"):
        data = doc.export_to_dict()
        blocks = data.get("blocks") or data.get("elements") or []
        for block in blocks:
            raw_bbox = block.get("bbox")
            bbox_rounded = None
            if raw_bbox is not None:
                bl = _bbox_to_list(raw_bbox)
                if bl and len(bl) >= 4:
                    bbox_rounded = [round(float(x), 1) for x in bl[:4]]
            objects.append(
                {
                    "type": block.get("category") or block.get("type") or "unknown",
                    "page": block.get("page_no") or block.get("page"),
                    "bbox": bbox_rounded,
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
    pipeline_options = PdfPipelineOptions(
        do_ocr=True,
        ocr_options=ocr_options,
        generate_picture_images=True,  # нужны для вывода сегментов изображений
    )
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        }
    )
    result = converter.convert(pdf_path)
    doc = result.document
    text = doc.export_to_text()
    objects = _extract_objects(doc)

    # Рендер страниц PDF в изображения для визуализации bbox (как в qwenbbox)
    pages_for_ui: List[Dict[str, Any]] = []
    try:
        page_images = convert_from_path(str(pdf_path), dpi=150)
        for i, pil_img in enumerate(page_images):
            page_num = i + 1
            w_px, h_px = pil_img.size
            w_pt = w_px * 72 / 150
            h_pt = h_px * 72 / 150
            buf = io.BytesIO()
            pil_img.save(buf, format="PNG")
            image_base64 = base64.b64encode(buf.getvalue()).decode("ascii")
            elements_on_page = []
            for obj in objects:
                if obj.get("page") != page_num:
                    continue
                bbox = obj.get("bbox")
                if not bbox or len(bbox) < 4:
                    elements_on_page.append({**obj, "bbox_norm": None})
                    continue
                left, top, right, bottom = [float(bbox[j]) for j in range(4)]
                # PDF: origin bottom-left; переводим в нормализованные 0–1 (top-left origin для canvas)
                x1 = left / w_pt
                x2 = right / w_pt
                y1 = 1.0 - (bottom / h_pt)
                y2 = 1.0 - (top / h_pt)
                elements_on_page.append({**obj, "bbox_norm": [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]})
            pages_for_ui.append({
                "page": page_num,
                "image_base64": image_base64,
                "image_width_px": w_px,
                "image_height_px": h_px,
                "elements": elements_on_page,
            })
    except Exception:  # noqa: BLE001
        pass

    return {"text": text, "objects": objects, "pages": pages_for_ui, "num_pages": len(pages_for_ui)}


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

