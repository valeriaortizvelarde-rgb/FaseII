from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path("/opt/package")
total_bytes = 0
with ZipFile("/tmp/ingest-lambda.zip", "w", compression=ZIP_DEFLATED) as archive:
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or "__pycache__" in relative.parts or "tests" in relative.parts:
            continue
        if path.suffix == ".pyc":
            continue
        total_bytes += path.stat().st_size
        archive.write(path, relative)
if total_bytes > 250 * 1024 * 1024:
    raise ValueError("El paquete Lambda supera el limite descomprimido de 250 MiB.")
print(f"Paquete Lambda: {total_bytes // (1024 * 1024)} MiB descomprimidos")