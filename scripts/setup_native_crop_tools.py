"""Install pinned local crop tools under pilot/model_cache; requires a JDK."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.request import Request, urlopen
import zipfile


ROOT = Path(__file__).resolve().parents[1] / "pilot/model_cache/crop-tools"
PDFFIGURES = "3d7ad46753d4a315cccd1c2bcab398380e88c534"
CLASSIFIER = "f859dfbff5c9916cd996942d4b0db7fa25808220"
MODEL_FILES = {
    "config.json": "0aecf700b276034667c64049cfef382ff8d92000",
    "preprocessor_config.json": "557b4156d6d0231194df86bcf22705455026946d",
    "model.safetensors": "6bf1e44d6bce316dcade6eb9929d8f8d23b6e8d9d29062b3b4011cff87c7c3cd",
}


def download(url):
    with urlopen(Request(url, headers={"User-Agent":"native-crop-tools/1.0"}), timeout=45) as response:
        return response.read()


def classifier(dest):
    dest.mkdir()
    for name, expected in MODEL_FILES.items():
        model = "docling-project/DocumentFigureClassifier-v2.5"
        urls = [f"https://{host}/{model}/resolve/{CLASSIFIER}/{name}?download=true" for host in
                ("huggingface.co","hf-mirror.com")]
        urls.append(f"https://modelscope.cn/models/{model}/resolve/master/{name}?download=true")
        for url in urls:
            try:
                data = download(url)
                break
            except OSError:
                if url == urls[-1]:
                    raise
        digest = (hashlib.sha256(data).hexdigest() if len(expected) == 64 else
                  hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest())
        if digest != expected:
            raise ValueError(f"Wrong classifier artifact: {name}")
        (dest / name).write_bytes(data)


def pdffigures(dest):
    dest.mkdir()
    jars = dest / "lib"
    jars.mkdir()
    coords = [
        ("org.scala-lang", name, "2.12.20") for name in ("scala-library", "scala-reflect", "scala-compiler")
    ] + [
        ("io.spray", "spray-json_2.12", "1.3.6"), ("com.github.scopt", "scopt_2.12", "4.1.0"),
        ("ch.qos.logback", "logback-classic", "1.2.11"), ("ch.qos.logback", "logback-core", "1.2.11"),
        ("org.slf4j", "slf4j-api", "1.7.36"), ("org.slf4j", "jcl-over-slf4j", "1.7.36"),
        ("org.apache.pdfbox", "pdfbox", "2.0.26"), ("org.apache.pdfbox", "fontbox", "2.0.26"),
        ("com.typesafe", "config", "1.4.2"),
    ] + [("org.bouncycastle", name + "-jdk18on", "1.71") for name in ("bcprov","bcmail","bcpkix","bcutil")]
    def jar(coord):
        group, artifact, version = coord
        name = f"{artifact}-{version}.jar"
        url = f"https://repo.maven.apache.org/maven2/{group.replace('.', '/')}/{artifact}/{version}/{name}"
        data = download(url)
        if hashlib.sha1(data).hexdigest() != download(url+".sha1").decode().strip():
            raise ValueError(f"Invalid Maven artifact: {name}")
        (jars / name).write_bytes(data)
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(jar, coords))
    archive = download("https://codeload.github.com/allenai/pdffigures2/zip/" + PDFFIGURES)
    classes = dest.parent / "classes"
    classes.mkdir()
    sources = []
    java_sources = []
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        for name in z.namelist():
            if name.endswith("/") or "/src/main/" not in name:
                continue
            key = name.split("/src/main/", 1)[1]
            if key.endswith((".scala", ".java")):
                target = dest.parent / "source" / key
                (java_sources if key.endswith(".java") else sources).append(target)
            elif key.startswith("resources/"):
                target = classes / key.removeprefix("resources/")
            else:
                continue
            if not target.resolve().is_relative_to(dest.parent):
                raise ValueError("Invalid archive path")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(name))
    if java_sources:
        subprocess.run(["javac", "--release", "8", "-encoding", "UTF-8", "-cp", str(jars/"*"),
                        "-d", str(classes), *map(str,java_sources)], check=True)
    subprocess.run(["java", "-cp", os.pathsep.join((str(jars/"*"),str(classes))), "scala.tools.nsc.Main", "-usejavacp",
                    "-encoding", "UTF-8", "-d", str(classes), *map(str,sources)], check=True)
    subprocess.run(["jar", "cf", str(jars/"pdffigures2.jar"), "-C",str(classes),"."], check=True)
    (jars / "scala-compiler-2.12.20.jar").unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=("classifier","pdffigures","all"), default="all", nargs="?")
    choice = parser.parse_args().tool
    ROOT.mkdir(exist_ok=True, parents=True)
    for name, build in (("pdffigures", pdffigures), ("classifier", classifier)):
        if choice not in ("all",name) or (ROOT/name).exists():
            continue
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            dest = Path(tmp) / name
            build(dest)
            dest.rename(ROOT/name)
            print("Installed", ROOT/name, flush=True)
