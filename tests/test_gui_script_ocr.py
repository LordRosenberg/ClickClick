import subprocess

from PIL import Image

from evaluation.gui_script_ocr import LocalScriptOCR


def test_local_ocr_preserves_word_and_line_bounds_on_one_frame(tmp_path, monkeypatch):
    executable = tmp_path / "tesseract"
    executable.touch()
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\ttext"
    output = ("\n".join((header, "5\t1\t1\t1\t1\t10\t8\t30\t9\tProject",
                          "5\t1\t1\t1\t1\t44\t9\t8\t7\tA",
                          "5\t1\t1\t1\t2\t70\t21\t45\t9\tOutside"))).encode()
    calls = []

    def run(command, **options):
        calls.append(command)
        assert options["input"].startswith(b"\x89PNG")
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(subprocess, "run", run)
    read = LocalScriptOCR(executable, "eng")
    image = Image.new("RGB", (100, 40))
    assert read(image) == (("Project", (10, 8, 40, 17)), ("A", (44, 9, 52, 16)),
                           ("Project A", (10, 8, 52, 17)))
    assert read(image.copy()) == read(image)
    assert len(calls) == 1
    assert calls[0][-3:] == ["-l", "eng", "tsv"]
