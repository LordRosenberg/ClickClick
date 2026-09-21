"""Audited AndroidWorld scoring; acquisition errors are not task failures."""
from contextlib import contextmanager
import json
import hashlib
from pathlib import Path
import threading
import uuid
from xml.etree import ElementTree
import time


POLICY = "official-predicate-native-forest-v4"
_DUMP_LOCK = threading.RLock()


class OracleObservationError(RuntimeError):
    """The oracle could not obtain a fresh, usable UI hierarchy."""


def excluded_oracle_error(result: dict) -> bool:
    return (result.get("infrastructure_failure") == "oracle_observation_failed"
            and result.get("score") is None and result.get("valid") is False
            and result.get("teardown_ok") is True and not result.get("teardown_error"))


def fresh_uiautomator_dump(env, timeout_sec, *, request, ok_status, evidence, directory,
                          oracle_jar=None, observation_format="xml"):
    # Never read the shared window_dump.xml: a failed dump leaves it untouched.
    token = uuid.uuid4().hex
    if observation_format not in ("xml", "forest") or (observation_format == "forest" and oracle_jar is None):
        raise OracleObservationError("Native forest requires the independent oracle helper")
    suffix = "json" if observation_format == "forest" else "xml"
    remote = f"/sdcard/clickclick-oracle-{token}.{suffix}"
    record = {"remote_path": remote, "commands": [], "accepted": False}
    evidence.append(record)

    def call(args):
        row = {"args": args}
        record["commands"].append(row)
        try:
            response = request(args, env, timeout_sec=timeout_sec)
        except Exception as exc:
            row["error"] = repr(exc)
            raise OracleObservationError(f"Oracle ADB request failed: {args}") from exc
        output = response.generic.output.decode("utf-8", errors="replace")
        row.update(status=int(response.status), output=output,
                   error_message=response.error_message)
        if response.status != ok_status:
            raise OracleObservationError(f"Oracle ADB response failed: {args}: {output}")
        return output

    try:
        helper = None
        if oracle_jar is not None:
            helper = f"/data/local/tmp/clickclick-oracle-{token}.jar"
            record["provider"] = "independent-uiautomation-no-idle"
            record["helper_sha256"] = hashlib.sha256(oracle_jar.read_bytes()).hexdigest()
            call(["push", str(oracle_jar), helper])
            # All shell tokens are generated paths/UUIDs, never task input.
            seconds = max(1, min(20, int(timeout_sec) - 2))
            output = call(["shell", f"CLASSPATH={helper}:/system/framework/uiautomator.jar",
                           "timeout", str(seconds), "app_process", "/system/bin", "OracleDump", remote, token,
                           *(["forest"] if observation_format == "forest" else [])])
            if output.strip() != f"ORACLE_DUMP_OK {token}":
                raise OracleObservationError(f"Independent oracle did not produce a validated dump: {output.strip()}")
        else:
            output = call(["shell", "uiautomator", "dump", remote])
        # UIAutomator may report adb OK even for 'could not get idle state'.
        if oracle_jar is None and ("error" in output.lower() or f"dumped to: {remote}" not in output):
            raise OracleObservationError(f"UIAutomator did not produce a fresh dump: {output.strip()}")
        xml = call(["shell", "cat", remote])
        xml_path = directory / f"{token}.{suffix}"
        xml_path.write_text(xml, encoding="utf-8")
        record[f"{suffix}_file"] = xml_path.name
        if observation_format == "forest":
            parse_oracle_forest(xml)
            record["accepted"] = True
            return xml
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError as exc:
            raise OracleObservationError("Oracle XML is malformed") from exc
        if root.tag != "hierarchy" or root.find(".//node") is None:
            raise OracleObservationError("Oracle XML has no usable hierarchy")
        record["accepted"] = True
        return xml
    except Exception as exc:
        record["error"] = repr(exc)
        raise
    finally:
        try:
            call(["shell", "rm", "-f", remote, *([helper] if helper else [])])
        except OracleObservationError as exc:
            record["cleanup_error"] = str(exc)


def parse_oracle_forest(payload):
    """Validate required native fields, then use the official protobuf type."""
    from android_env.proto.a11y import android_accessibility_forest_pb2
    from google.protobuf.json_format import ParseDict, ParseError

    try:
        data = json.loads(payload)
        if data.pop("format") != "oracle-forest-v1" or not data["windows"]:
            raise ValueError("missing windows")
        for window in data["windows"]:
            nodes = window["tree"]["nodes"]
            if not nodes:
                raise ValueError("empty window")
            incoming = [0] * len(nodes)
            for i, node in enumerate(nodes):
                if node.pop("id") != i:
                    raise ValueError("invalid node ordinal")
                strings = ('text', 'hint_text', 'content_description', 'class_name', 'package_name', 'view_id_resource_name')
                booleans = ('is_checked', 'is_checkable', 'is_clickable', 'is_editable', 'is_enabled', 'is_focused',
                            'is_focusable', 'is_long_clickable', 'is_scrollable', 'is_selected', 'is_visible_to_user')
                if not all(isinstance(node[k], str) for k in strings):
                    raise ValueError("missing native text")
                if not all(type(node[k]) is bool for k in booleans):
                    raise ValueError("missing native state")
                if not all(type(node['bounds_in_screen'][k]) is int for k in ('left', 'right', 'top', 'bottom')):
                    raise ValueError("missing native bounds")
                if not isinstance(node['child_ids'], list):
                    raise ValueError("missing children")
                for child in node['child_ids']:
                    if type(child) is not int or not i < child < len(nodes):
                        raise ValueError("invalid child")
                    incoming[child] += 1
            if incoming != [0] + [1] * (len(nodes) - 1):
                raise ValueError("disconnected or duplicate children")
        return ParseDict(data, android_accessibility_forest_pb2.AndroidAccessibilityForest())
    except (ValueError, KeyError, TypeError, AttributeError, ParseError) as exc:
        raise OracleObservationError("Native oracle forest is missing required fields or malformed") from exc


def read_fresh_forest(controller, directory, evidence):
    from android_env.proto import adb_pb2
    from android_world.env import adb_utils
    jar = Path(__file__).with_name("oracle") / "oracle.jar"
    if not jar.is_file():
        raise OracleObservationError("Missing frozen oracle.jar; build/freeze evaluation first")
    # Retry only acquisition, never the scoring predicate or model actions.
    for attempt in range(3):
        try:
            payload = fresh_uiautomator_dump(
                controller, 12, request=adb_utils.issue_generic_request,
                ok_status=adb_pb2.AdbResponse.Status.OK, evidence=evidence, directory=directory,
                oracle_jar=jar, observation_format="forest")
            return parse_oracle_forest(payload)
        except OracleObservationError:
            if attempt == 2:
                raise
            time.sleep(0.2)


@contextmanager
def fresh_oracle_observations(env, directory: Path, evidence: list):
    from android_world.env import android_world_controller

    # Preserve the official controller's State assembly and forest conversion.
    # Only the transport is replaced, for the lifetime of this scoring call.
    with _DUMP_LOCK:
        controller = env.controller
        original_method = controller._a11y_method
        had_override = "get_a11y_forest" in vars(controller)
        original_override = vars(controller).get("get_a11y_forest")
        controller.get_a11y_forest = lambda: read_fresh_forest(controller, directory, evidence)
        controller._a11y_method = android_world_controller.A11yMethod.A11Y_FORWARDER_APP
        try:
            yield
        finally:
            controller._a11y_method = original_method
            if had_override:
                controller.get_a11y_forest = original_override
            else:
                del controller.get_a11y_forest


def score_task(task, env, output: Path, phase: str) -> dict:
    """Call the unmodified official predicate exactly once; errors have no score."""
    directory = output / f"{phase}-oracle"
    directory.mkdir(parents=True, exist_ok=False)
    audit = {"scoring_policy": POLICY, "observations": []}
    try:
        with fresh_oracle_observations(env, directory, audit["observations"]):
            raw = float(task.is_successful(env))
            result = {"score": raw, "raw_official_score": raw, "scoring_policy": POLICY}
            audit.update(result)
            return result
    except Exception as exc:
        audit["error"] = repr(exc)
        raise
    finally:
        (directory / "audit.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
