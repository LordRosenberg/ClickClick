"""Build the API 33 oracle command from source; no checked-in executable."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def build_oracle(source: Path, output: Path) -> None:
    java_home = Path(os.environ["JAVA_HOME"])
    sdk = Path(os.environ.get("ANDROID_HOME") or os.environ["ANDROID_SDK_ROOT"])
    platforms = sorted(sdk.glob("platforms/android-*/android.jar"),
                       key=lambda p: int(p.parent.name.split("-")[-1].split(".")[0]))
    dexers = sorted(sdk.glob("build-tools/*/lib/d8.jar"),
                    key=lambda p: tuple(int(x) for x in p.parent.parent.name.split(".") if x.isdigit()))
    if not platforms or not dexers:
        raise RuntimeError("Install an Android SDK platform and build-tools before freezing evaluation")
    suffix = ".exe" if os.name == "nt" else ""
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        subprocess.run([str(java_home / "bin" / ("javac" + suffix)), "-source", "8", "-target", "8",
                        "-cp", str(platforms[-1]), "-d", directory, str(source)], check=True)
        subprocess.run([str(java_home / "bin" / ("java" + suffix)), "-cp", str(dexers[-1]),
                        "com.android.tools.r8.D8", "--lib", str(platforms[-1]), "--output", str(output),
                        str(Path(directory) / "OracleDump.class")], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build_oracle(Path(__file__).with_name("oracle") / "OracleDump.java", args.output)
