'''
ICEYE Demo: GRD vs SLC
----------------------
Goal: load one ICEYE scene straight from the AWS bucket.
Compare the GRD and SLC products side by side.
 
'''
#Libraries
import os
import sys

import matplotlib.pyplot as plt
import rasterio
import numpy as np
import pystac
from rasterio.windows import Window

CATALOG_URL = "https://iceye-open-data-catalog.s3-us-west-2.amazonaws.com/catalog.json"
MAX_ITEMS = 60 # how many scenes to list
CROP_X = 0.25   # crop centre, as a fraction of image width  (0 = left,  1 = right)
CROP_Y = 0.30   # crop centre, as a fraction of image height (0 = top,   1 = bottom)
CROP = 1024 #size of the SLC crop in pixels (full SLCs are large)

#Faster remote heads of Cloud-Optimised GeoTIFFS
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")

def to_https(href: str) -> str:
    """
    Turn s3://bucket/key into a public URL rasterio can stream.
    """
    if href.startswith("s3://"):
        bucket, key = href[5:].split("/", 1)
        return f"https://{bucket}.s3.us-west-2.amazonaws.com/{key}"
    return href

def list_scenes():
    print("Reading the ICEYE STAC catalog.....")
    catalog = pystac.Catalog.from_file(CATALOG_URL)
    items = []
    for item in catalog.get_items(recursive=True):
        items.append(item)
        if len(items) >= MAX_ITEMS:
            break
    return items

def find_asset(item, word):
    """
    Pick the first .tif asset whose key or file name contains 'word'.

    """
    for key, asset in item.assets.items():
        href = asset.get_absolute_href() or asset.href
        name = (key + " " + href).lower()
        if word in name and href.lower().endswith((".tif", ".tiff")):
            return to_https(href)
    return None

def to_db(amplitude):
    """Amplitude -> decibels, the usual way to display SAR images."""
    return 20 * np.log10(np.maximum(amplitude, 1e-6))


def stretch(img):
    lo, hi = np.nanpercentile(img, [2, 98])
    return np.clip((img - lo) / (hi - lo + 1e-9), 0, 1)


def read_grd(url, max_side=2000):
    """GRD is an amplitude image. Read a reduced version using the COG overviews."""
    with rasterio.open(url) as src:
        factor = max(1, max(src.width, src.height) // max_side)
        data = src.read(1, out_shape=(src.height // factor, src.width // factor)).astype("float32")
        info = dict(size=(src.width, src.height), dtype=src.dtypes[0], crs=str(src.crs), res=src.res, factor=factor)
    return data, info

def read_grd_crop(url, size=CROP):
    """Full-resolution crop from the centre of the GRD (same pixel size as the SLC crop)."""
    with rasterio.open(url) as src:
        win = Window(int(src.width * CROP_X) - size // 2, int(src.height * CROP_Y) - size // 2, size, size)
        data = src.read(1, window=win).astype("float32")
    return data, win

SLC_SQUASH = 8   # columns averaged together; tune until features look natural

def read_slc_crop(url, size=CROP, squash=SLC_SQUASH):
    """Read a wide SLC strip and average columns so pixels become roughly square."""
    with rasterio.open(url) as src:
        cx, cy = int(src.width * CROP_X), int(src.height * CROP_Y)
        win = Window(cx - size * squash // 2, cy - size // 2, size * squash, size)
        b1, b2 = src.read([1, 2], window=win).astype("float32")
        print("  band 1 range:", b1.min(), b1.max(), "| band 2 range:", b2.min(), b2.max())
        info = dict(size=(src.width, src.height), dtype=src.dtypes[0], bands=src.count,
                    descriptions=src.descriptions)
    slc_full = b1 * np.exp(1j * b2)   # band 1 = amplitude, band 2 = phase
    intensity = np.abs(slc_full) ** 2
    amp = np.sqrt(intensity.reshape(size, size, squash).mean(axis=2))   # squashed, roughly square
    return slc_full[:, :size], amp, info


def multilook(amplitude, n=4):
    """Average n x n pixels of intensity: trades resolution for less speckle."""
    h, w = (amplitude.shape[0] // n) * n, (amplitude.shape[1] // n) * n
    intensity = amplitude[:h, :w] ** 2
    return np.sqrt(intensity.reshape(h // n, n, w // n, n).mean(axis=(1, 3)))

def run_demo(item):
    grd_url, slc_url = find_asset(item, "grd"), find_asset(item, "slc")
    print(f"\nScene: {item.id}\n  GRD: {grd_url}\n  SLC: {slc_url}")
    print("  Location (lon/lat bounding box):", item.bbox)
    if not grd_url or not slc_url:
        print("This scene does not have both a GRD and an SLC file. Pick another number.")
        return

    grd, grd_info = read_grd(grd_url)
    grd_crop, grd_win = read_grd_crop(grd_url)
    slc, amp, slc_info = read_slc_crop(slc_url)
    phase = np.angle(slc)    
    print("  GRD info:", grd_info)
    print("  SLC info:", slc_info)

    # Figure 1: the three-panel comparison
    fig, ax = plt.subplots(1, 3, figsize=(18, 6.5))
    ax[0].imshow(stretch(to_db(grd)), cmap="gray")
    f = grd_info["factor"]  # the overview is shrunk by this factor, so scale the box down too
    ax[0].add_patch(plt.Rectangle((grd_win.col_off / f, grd_win.row_off / f), CROP / f, CROP / f,
                                  fill=False, edgecolor="red", linewidth=2))
    ax[0].set_title("GRD, whole scene (reduced)\nred box = area of the crops")
    ax[1].imshow(stretch(to_db(grd_crop)), cmap="gray")
    ax[1].set_title(f"GRD crop ({CROP}x{CROP}, full resolution)\nground geometry, smoother")
    ax[2].imshow(stretch(to_db(amp)), cmap="gray")
    ax[2].set_title(f"SLC amplitude crop (columns averaged {SLC_SQUASH}x to square pixels)\nslant-range geometry, more speckle")
    for a in ax[1:]:
        a.axis("off")
    fig.suptitle(f"ICEYE open data: {item.id}  |  crops are only approximately the same area "
                 f"(different geometry)\nData: ICEYE, CC-BY 4.0", fontsize=11)
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    out = f"iceye_{item.id}_grd_vs_slc.png"
    fig.savefig(out, dpi=150)
    print(f"\nSaved figure: {out}")

    # Figure 2: what is special about SLC (phase and speckle reduction)
    fig2, ax2 = plt.subplots(1, 2, figsize=(12, 6.5))
    ax2[0].imshow(phase, cmap="twilight")
    ax2[0].set_title("SLC phase\nlooks random alone; useful when comparing two images")
    ax2[1].imshow(stretch(to_db(multilook(amp, 4))), cmap="gray")
    ax2[1].set_title("SLC after 4x4 multilooking\nless speckle, lower resolution")
    for a in ax2:
        a.axis("off")
    fig2.suptitle(f"ICEYE open data: {item.id}\nData: ICEYE, CC-BY 4.0", fontsize=11)
    fig2.tight_layout(rect=[0, 0, 1, 0.93])
    out2 = f"iceye_{item.id}_slc_phase_multilook.png"
    fig2.savefig(out2, dpi=150)
    print(f"Saved figure: {out2}")


if __name__ == "__main__":
    scenes = list_scenes()
    if len(sys.argv) < 2:
        for i, it in enumerate(scenes):
            mode = it.properties.get("sar:instrument_mode") or it.properties.get("iceye:product_type") or ""
            print(f"[{i:2d}] {it.id}  {it.datetime}  {mode}")
            print("      files:", ", ".join(it.assets.keys()))
        print("\nRun again with a scene number, e.g.:  python iceye_grd_vs_slc_demo.py 3")
    else:
        run_demo(scenes[int(sys.argv[1])])
