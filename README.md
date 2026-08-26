# learncpp2pdf

Builds the [learncpp.com](https://www.learncpp.com/) C++ tutorial into a single
PDF with working internal hyperlinks.

learncpp.com has no official PDF. Its FAQ permits converting pages to PDF for
your own private use, but not distributing the result — so this repository
contains the tool, not the book. Run it yourself, keep the output to yourself,
and if the tutorial is useful to you, consider supporting the author at
[learncpp.com/about](https://www.learncpp.com/about/).

## What you get

- **One PDF**
- **Working cross-references** 
- **Clickable contents with page numbers**
- **PDF bookmarks**
- **Solutions kept out of sight** 
- **Static syntax highlighting** 

Roughly 2500 A4 pages, 15 MB.
A full run on a free Colab instance takes
about 65 minutes end to end.

## Install

```bash
pip install requests beautifulsoup4 lxml pygments pillow weasyprint tqdm
```

WeasyPrint needs Pango and HarfBuzz. On Debian/Ubuntu:

```bash
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
                 libfribidi0 fonts-dejavu-core qpdf
```

`qpdf` is optional; if present, the output gets a recompression pass.

## Usage

```bash
python learncpp2pdf.py --limit 15     # quick trial run, ~90 pages
python learncpp2pdf.py                # the whole book
```

Pages are cached on disk, so the second run doesn't re-download anything.

| Option | Default | Notes |
| --- | --- | --- |
| `-o, --out` | `LearnCpp.pdf` | output path |
| `--cache-dir` | `cache` | downloaded HTML and images |
| `--engine` | `weasyprint` | or `wkhtmltopdf` |
| `--limit N` | all | build only the first N lessons |
| `--solutions` | `appendix` | or `inline` |
| `--no-images` | off | smaller file |
| `--max-image-width` | 900 | px, images are downscaled and recompressed |
| `--workers` | 4 | parallel downloads |
| `--delay` | 0.4 | seconds between requests, shared across workers |
| `--refresh` | off | ignore the cache |
| `--html-only` | off | stop after `book.html`, for debugging layout |

## Google Colab

`LearnCpp_to_PDF.ipynb` is a ready notebook: it installs the system libraries,
optionally mounts Drive for the cache so a runtime disconnect doesn't cost you
the download, and verifies the finished PDF. Upload it via File → Upload
notebook.

## Notes

**Start with `--limit`.** If the site markup changes, a 15-lesson run tells you
in under a minute instead of after a full build.

## License

MIT for the code in this repository. The tutorial content belongs to
learncpp.com and is not covered by it.
