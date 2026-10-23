# Third-party notices

Absolute Flight's own code is MIT licensed (see [LICENSE](LICENSE)). The release zip also ships the
third-party software below, each under its own licence. All of them allow redistribution.

Where the full licence texts are in the zip:

- **Python:** `python\LICENSE.txt`.
- **Python packages:** each package's `python\Lib\site-packages\<package>-<version>.dist-info\` folder
  (a `LICENSE` file or a `licenses\` subfolder).
- **Rust crates** in `bin\absolute_input_bus.dll`: the MIT licence text is below. The crates' sources are
  at the links given.

## Python runtime

| Software | Licence | Source |
|---|---|---|
| Python 3.11 (Python Software Foundation) | PSF-2.0 | https://www.python.org |

## Python packages (in `python\Lib\site-packages`)

| Package | Author | Licence | Source |
|---|---|---|---|
| NMSpy (NMS.py) | monkeyman192 | MIT | https://github.com/monkeyman192/NMS.py |
| pyMHF | monkeyman192 | MIT | https://github.com/monkeyman192/pyMHF |
| pyrun_injected | monkeyman192 | MIT | https://github.com/monkeyman192/pyrun_injected |
| annotated-doc | Sebastián Ramírez | MIT | https://github.com/fastapi/annotated-doc |
| annotated-types | Adrian Garcia Badaracco, Samuel Colvin, Zac Hatfield-Dodds | MIT | https://github.com/annotated-types/annotated-types |
| anyio | Alex Grönholm | MIT | https://github.com/agronholm/anyio |
| capstone (includes the Capstone engine) | Nguyen Anh Quynh | BSD-3-Clause | https://www.capstone-engine.org |
| click | Pallets | BSD-3-Clause | https://github.com/pallets/click |
| cyminhook (includes MinHook by Tsuda Kageyu) | Segev Finer | MIT; MinHook BSD-2-Clause | https://github.com/segevfiner/cyminhook |
| dearpygui | Jonathan Hoffstadt, Preston Cothren | MIT | https://github.com/hoffstadt/DearPyGui |
| fastapi | Sebastián Ramírez | MIT | https://github.com/fastapi/fastapi |
| h11 | Nathaniel J. Smith | MIT | https://github.com/python-hyper/h11 |
| iced-x86 | wtfsck | MIT | https://github.com/icedland/iced |
| idna | Kim Davies | BSD-3-Clause | https://github.com/kjd/idna |
| keyboard | BoppreH | MIT | https://github.com/boppreh/keyboard |
| opentelemetry-api | OpenTelemetry Authors | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python |
| packaging | Donald Stufft and contributors | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging |
| pefile | Ero Carrera | MIT | https://github.com/erocarrera/pefile |
| prompt_toolkit | Jonathan Slenders | BSD-3-Clause | https://github.com/prompt-toolkit/python-prompt-toolkit |
| psutil | Giampaolo Rodola | BSD-3-Clause | https://github.com/giampaolo/psutil |
| pydantic | Samuel Colvin and contributors | MIT | https://github.com/pydantic/pydantic |
| pydantic_core | Samuel Colvin and contributors | MIT | https://github.com/pydantic/pydantic |
| Pymem | Fabien Reboia | MIT | https://github.com/srounet/Pymem |
| PyMonCtl | Kalmat | BSD-3-Clause | https://github.com/Kalmat/PyMonCtl |
| pywin32 | Mark Hammond and others | PSF-2.0 | https://github.com/mhammond/pywin32 |
| PyWinBox | Kalmat | BSD-3-Clause | https://github.com/Kalmat/PyWinBox |
| PyWinCtl | Kalmat | BSD-3-Clause | https://github.com/Kalmat/PyWinCtl |
| questionary | Tom Bocklisch | MIT | https://github.com/tmbo/questionary |
| regex | Matthew Barnett | Apache-2.0 | https://github.com/mrabarnett/mrab-regex |
| starlette | Tom Christie and contributors | BSD-3-Clause | https://github.com/Kludex/starlette |
| tomlkit | Sébastien Eustace | MIT | https://github.com/python-poetry/tomlkit |
| typing-inspection | Victorien Plot | MIT | https://github.com/pydantic/typing-inspection |
| typing_extensions | Guido van Rossum, Jukka Lehtosalo, Łukasz Langa, Michael Lee | PSF-2.0 | https://github.com/python/typing_extensions |
| uvicorn | Tom Christie and contributors | BSD-3-Clause | https://github.com/Kludex/uvicorn |
| wcwidth | Jeff Quast | MIT | https://github.com/jquast/wcwidth |
| zstandard | Gregory Szorc | BSD-3-Clause | https://github.com/indygreg/python-zstandard |

## Rust crates (in `bin\absolute_input_bus.dll`)

Each is offered under MIT OR Apache-2.0; Absolute Flight uses them under MIT.

| Crate | Author | Source |
|---|---|---|
| windows, windows-collections, windows-core, windows-future, windows-link, windows-numerics, windows-result, windows-strings, windows-threading | Microsoft | https://github.com/microsoft/windows-rs |
| windows-implement, windows-interface (build-time macros, not in the DLL) | Microsoft | https://github.com/microsoft/windows-rs |
| proc-macro2, quote, syn (build-time, not in the DLL) | David Tolnay, Alex Crichton | https://github.com/dtolnay |
| unicode-ident (build-time, not in the DLL; also Unicode-3.0 for its data) | David Tolnay | https://github.com/dtolnay/unicode-ident |

MIT licence text for the windows-rs crates:

```
MIT License

Copyright (c) Microsoft Corporation.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Voice lines (`mod\voice\*.wav`)

Rendered offline from the Piper voice **en_GB-cori-high** by Bryce Beattie, trained on public-domain
LibriVox recordings (https://huggingface.co/rhasspy/piper-voices). The voice model and the Piper engine
are not shipped; only the rendered audio is.

## No Man's Sky

No Man's Sky is a trademark of Hello Games. Absolute Flight is an unofficial fan mod, not made, endorsed
or supported by Hello Games. It ships no game files or game assets.
