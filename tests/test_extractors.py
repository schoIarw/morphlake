import io

import pytest
from PIL import Image

from morphlake.errors import MorphLakeError
from morphlake.services.extractors import chunk_text, classify, create_thumbnail, extract_text


@pytest.mark.parametrize(
    ("filename", "expected"),
    [("a.pdf", "document"), ("a.JPG", "image"), ("a.mp3", "audio")],
)
def test_classify_supported_media(filename, expected):
    assert classify(filename) == expected


def test_classify_rejects_unknown_media():
    with pytest.raises(MorphLakeError) as exc:
        classify("archive.zip")
    assert exc.value.status_code == 415


def test_extract_plain_text_and_chunk_overlap():
    text = extract_text("note.txt", "第一段。\n\n第二段。".encode())
    chunks = chunk_text(text, size=8, overlap=2)
    assert text == "第一段。\n\n第二段。"
    assert len(chunks) >= 2
    assert chunks[0].index == 0
    assert chunks[1].start < chunks[0].end


def test_chunking_rejects_invalid_overlap():
    with pytest.raises(ValueError):
        chunk_text("abc", size=3, overlap=3)


def test_thumbnail_is_bounded_jpeg():
    source = io.BytesIO()
    Image.new("RGBA", (1200, 600), (20, 80, 180, 120)).save(source, "PNG")
    thumbnail = create_thumbnail(source.getvalue(), 320)
    with Image.open(io.BytesIO(thumbnail)) as rendered:
        assert rendered.format == "JPEG"
        assert rendered.size == (320, 160)
        assert rendered.mode == "RGB"


def test_thumbnail_rejects_invalid_image():
    with pytest.raises(MorphLakeError) as exc:
        create_thumbnail(b"not-an-image", 320)
    assert exc.value.code == "invalid_image"
