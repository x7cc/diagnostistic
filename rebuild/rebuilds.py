import json
import os
import shutil


def rebuild_zip(manifest_file="manifest.json"):
    if not os.path.exists(manifest_file):
        print("manifest.json not found.")
        return

    with open(manifest_file, "r") as f:
        manifest = json.load(f)

    output_name = manifest.get("final_zip_name")
    parts = manifest.get("chunks")

    if not output_name:
        print("manifest.json is missing 'final_zip_name'.")
        return
    if not parts or not isinstance(parts, list):
        print("manifest.json is missing or has invalid 'chunks'.")
        return

    try:
        with open(output_name, "wb") as outfile:
            for part in parts:
                if not os.path.exists(part):
                    raise FileNotFoundError(f"Chunk not found: {part}")
                print(f"Merging {part}...")
                with open(part, "rb") as infile:
                    shutil.copyfileobj(infile, outfile)
    except Exception as e:
        if os.path.exists(output_name):
            os.remove(output_name)
        print(f"Error during rebuild: {e}")
        return

    print(f"\nRebuild complete! Output file: {output_name}")


if __name__ == "__main__":
    rebuild_zip()
