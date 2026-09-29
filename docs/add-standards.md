# Adding the robotics and AI standards

These standards are not published as open registers, so they have to be added
one at a time from pages you open in your own browser. Each is one save and
one command.

The ISO OBP is behind bot protection, so there is no automated fetch here by
design. The [OBP Terms of Use](https://www.iso.org/home/store/iso-obp-terms.html)
govern reuse of the content.

## Procedure

1. Open the OBP link below. The "Terms and definitions" clause is in the free
   preview; normative clauses are paywalled and not needed.
2. Expand the terms section, then save the page as HTML
   (<kbd>Ctrl</kbd>+<kbd>S</kbd>, "Web page, HTML only").
3. Run the converter with the URN and reference from the table:

   ```bash
   scripts/obp-html-to-sts.sh ~/Downloads/iso8373.html \
     iso:std:iso:8373:ed-3:v1:en "ISO 8373:2021"
   ```

4. Check it landed: `iso-obp-index info`

ISO 8373 is the best one to start with: its URN is the one confirmed working
against `obp-access`, so a problem on your first attempt is more likely to be
the saved HTML than the URN.

**If an OBP link below does not resolve**, the URNs here were derived from
ISO's catalogue metadata rather than read off OBP itself, so an edition or
part segment may be off. Search for the standard in the
[OBP UI](https://www.iso.org/obp/ui/#search), open it, and copy the fragment
after the `#` from the address bar. That string is the URN; pass it to the
script as-is. The URN only labels the output, so a corrected one works
immediately.

If a saved page yields zero entries, the terms markup was not in the saved
HTML: re-save with the terms clause expanded. The converter looks for
`div.sts-standard` wrapping `div.sts-tbx-sec` entries.

## Robotics core

| Standard | Covers | URN |
| --- | --- | --- |
| [ISO 8373:2021](https://www.iso.org/obp/ui/#iso:std:iso:8373:ed-3:v1:en) | Robotics vocabulary. The central one: manipulator, end effector, degree of freedom, workspace, autonomy | `iso:std:iso:8373:ed-3:v1:en` |
| [ISO 10218-1:2025](https://www.iso.org/obp/ui/#iso:std:iso:10218:-1:ed-3:v1:en) | Safety requirements, industrial robots | `iso:std:iso:10218:-1:ed-3:v1:en` |
| [ISO 10218-2:2025](https://www.iso.org/obp/ui/#iso:std:iso:10218:-2:ed-2:v1:en) | Safety requirements, robot applications and cells | `iso:std:iso:10218:-2:ed-2:v1:en` |
| [ISO/TS 15066:2016](https://www.iso.org/obp/ui/#iso:std:iso:ts:15066:ed-1:v1:en) | Collaborative robots: collaborative operation, power and force limiting | `iso:std:iso:ts:15066:ed-1:v1:en` |
| [ISO 12100:2010](https://www.iso.org/obp/ui/#iso:std:iso:12100:ed-1:v1:en) | Safety of machinery: hazard, risk, safety function, protective measure | `iso:std:iso:12100:ed-1:v1:en` |
| [ISO 13482:2014](https://www.iso.org/obp/ui/#iso:std:iso:13482:ed-1:v1:en) | Personal care robots, relevant for human-robot interaction | `iso:std:iso:13482:ed-1:v1:en` |

## IT and AI vocabulary

Start with 22989: it is the one that defines the machine-learning terms the
current index is missing.

| Standard | Covers | URN |
| --- | --- | --- |
| [ISO/IEC 22989:2022](https://www.iso.org/obp/ui/#iso:std:iso-iec:22989:ed-1:v1:en) | AI concepts and terminology: machine learning, neural network, training, inference | `iso:std:iso-iec:22989:ed-1:v1:en` |
| [ISO/IEC TR 24028:2020](https://www.iso.org/obp/ui/#iso:std:iso-iec:tr:24028:ed-1:v1:en) | Trustworthiness in AI: robustness, explainability, bias | `iso:std:iso-iec:tr:24028:ed-1:v1:en` |
| [ISO/IEC 2382:2015](https://www.iso.org/obp/ui/#iso:std:iso-iec:2382:ed-1:v1:en) | Information technology vocabulary, consolidated | `iso:std:iso-iec:2382:ed-1:v1:en` |
| [ISO/IEC 2382-37:2022](https://www.iso.org/obp/ui/#iso:std:iso-iec:2382:-37:ed-3:v1:en) | Biometrics | `iso:std:iso-iec:2382:-37:ed-3:v1:en` |
| [ISO/IEC 2382-4:1999](https://www.iso.org/obp/ui/#iso:std:iso-iec:2382:-4:ed-1:v1:en) | Organization of data | `iso:std:iso-iec:2382:-4:ed-1:v1:en` |
| [ISO/IEC 2382-7:2000](https://www.iso.org/obp/ui/#iso:std:iso-iec:2382:-7:ed-3:v1:en) | Computer programming | `iso:std:iso-iec:2382:-7:ed-3:v1:en` |
| [ISO/IEC 2382-16:1996](https://www.iso.org/obp/ui/#iso:std:iso-iec:2382:-16:ed-2:v1:en) | Information theory | `iso:std:iso-iec:2382:-16:ed-2:v1:en` |

All URNs were derived from ISO's own open deliverables catalogue and every
standard listed is published and previewable.
