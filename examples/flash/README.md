# Example CCLOG01 inputs (synthetic)

The `.anchors.example.json` file is a hand-written, original synthetic anchor
document matching the image produced by
`tools/build_flash_examples.py` (its `scenario1` output). CCLOG01 is a
fictional protocol invented for this project; it is not a vendor format and
no real device data is included.

Regenerate matching binary images and anchors into a fresh directory:

```sh
python tools/build_flash_examples.py --output-dir examples/flash/generated
python -m cold_exposure.flash \
    --image examples/flash/generated/scenario1.bin \
    --anchors examples/flash/generated/scenario1.anchors.json \
    --output-dir run/flash/scenario1
```

Generated binary images are working artifacts and are not committed.
