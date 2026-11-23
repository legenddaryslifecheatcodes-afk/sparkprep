"""SparkPrep's independent checker (Checker 3's second opinion).

Separate on purpose from the main scanner (file_processor.py /
pdfx_validator.py / audit_engine.py):

* Its own code. Nothing in this package imports any SparkPrep module, and a
  test enforces that, so a bug in the main scanner can't quietly repeat here.
* Different tools. PDFs are read with pypdf (the main scanner uses pikepdf and
  PyMuPDF), and interior margins are measured from where Ghostscript actually
  puts ink on the rendered page (the main scanner reads PyMuPDF text blocks).
* Its own rule numbers, written below from the printers' published specs.
* Its own process. The app runs it with `python -E -s -m independent_checker`
  (see independent_check.py), with a memory cap, a time limit and no access
  to the database or any API keys. It reads the file, prints one JSON answer
  and exits.
"""
