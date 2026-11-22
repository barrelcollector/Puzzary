# Publishing and printing

Puzzary uses each installed module's print contract to create spoiler-free
browser layouts, PDF files, and PNG images. The web interface is the primary
place to print individual puzzles and assemble published issues.

## Print a puzzle

Open a puzzle and choose **Print**. The resulting sheet uses the module's
instructions and layout and can include the canonical online link or QR code.
Download PDF or PNG when those formats are enabled. Printing never exposes the
stored solution payload.

## Publish an issue

Under **Admin > Publishing**, create an issue, add printable puzzles and text
sections, arrange them, and preview the combined output. Publishing stores an
immutable ordered snapshot of the print content. Later edits, archival, module
updates, or removal of a source puzzle do not change that published edition.

To revise an issue, duplicate it into a new draft and publish the replacement.
Archiving removes an issue from ordinary availability without deleting its
history.

## Automation

External print services should request authorized print manifests and retrieve
the returned PDF or PNG URLs rather than reconstructing module layouts. Render
URLs are short-lived credentials and should not be stored or logged. Queueing,
printer status, cutter commands, and retry policies remain responsibilities of
the external print service.

See [API clients and print automation](api-clients.md) for authentication,
permissions, manifest selection, and issue endpoints. The deployed server's
`/docs` page is authoritative for the exact request and response schemas.
