# Third-party components

ClickClick's own source is licensed under [Apache-2.0](LICENSE). The following independently distributed components retain their upstream licenses.

| Component | Distribution | License and source |
| --- | --- | --- |
| scrcpy server 3.3.1 | `driver/vendor/scrcpy-server-v3.3.1.jar` | [Apache-2.0 license](driver/vendor/LICENSE.scrcpy.txt); [upstream source at v3.3.1](https://github.com/Genymobile/scrcpy/tree/v3.3.1). |
| ADBKeyboard | Separate APK in GitHub Releases | [GPL-2.0](https://github.com/senzhk/ADBKeyBoard/blob/d7b27288bc8c1c8348a79ea6896bd7352c30a9fc/LICENSE); [corresponding source](https://github.com/senzhk/ADBKeyBoard/tree/d7b27288bc8c1c8348a79ea6896bd7352c30a9fc), also attached as a source archive to the Release. |

The distributed ADBKeyboard APK matches the upstream binary at commit `d7b27288bc8c1c8348a79ea6896bd7352c30a9fc`, SHA-256 `e698adea5633135a067b038f9a0cf41baa4de09888713a81593fb2b9682cdc59`. It is an independent input-method application; its source archive includes its license and build files.

Python and JavaScript dependencies are installed from their package distributions and retain their individual licenses. Dependency versions are specified in `pyproject.toml` and `web/package-lock.json`.
