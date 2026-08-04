import os
import sys
import hashlib
import platform
import subprocess
import ctypes
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

def get_file_hash(filepath):
    if not filepath.exists():
        return "MISSING"
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def get_git_info():
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True, stderr=subprocess.DEVNULL).strip()
        status = subprocess.check_output(["git", "status", "--porcelain"], cwd=PROJECT_ROOT, text=True, stderr=subprocess.DEVNULL).strip()
        clean = len(status) == 0
        return {"commit": commit, "clean": clean, "status": status}
    except Exception as e:
        return {"error": str(e), "commit": None, "clean": None}

def get_sys_info():
    info = {
        "python_version": sys.version,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count()
    }
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        info["total_ram_gb"] = round(stat.ullTotalPhys / (1024**3), 2)
        info["avail_ram_gb"] = round(stat.ullAvailPhys / (1024**3), 2)
    except Exception as e:
        info["ram_error"] = str(e)
    return info

def get_package_versions():
    pkgs = ["torch", "transformers", "sentence-transformers", "scikit-learn", "numpy", "rapidfuzz", "google-generativeai", "pydantic"]
    versions = {}
    for p in pkgs:
        try:
            mod = __import__(p.replace("-", "_"))
            versions[p] = getattr(mod, "__version__", "unknown")
        except Exception as e:
            versions[p] = f"NOT INSTALLED ({e})"
    return versions

def main():
    print("=== PHASE 0 — VERIFY FROZEN EVIDENCE ===")
    
    files_to_check = [
        "evaluation/retrieval_benchmark/benchmark_manifest.json",
        "evaluation/retrieval_benchmark/split_manifest.json",
        "evaluation/retrieval_benchmark/frozen_config.json",
        "evaluation/retrieval_benchmark/benchmark_queries.jsonl",
        "evaluation/retrieval_benchmark/query_results.csv",
        "evaluation/retrieval_benchmark/method_metrics.csv",
        "evaluation/retrieval_benchmark/RETRIEVAL_BENCHMARK_REPORT.md",
        "evaluation/results/blind_predictions.jsonl",
        "evaluation/sealed/blind_ground_truth.jsonl",
        "evaluation/data/synthetic_catalog_400.csv"
    ]
    
    hashes = {}
    print("\n[File Hashes]")
    for rel_path in files_to_check:
        full_path = PROJECT_ROOT / rel_path
        h = get_file_hash(full_path)
        hashes[rel_path] = h
        print(f"{rel_path}: {h}")
        
    print("\n[Git Info]")
    git_info = get_git_info()
    print(json.dumps(git_info, indent=2))
    
    print("\n[System Info]")
    sys_info = get_sys_info()
    print(json.dumps(sys_info, indent=2))
    
    print("\n[Package Versions]")
    pkg_info = get_package_versions()
    print(json.dumps(pkg_info, indent=2))
    
    output_data = {
        "file_hashes": hashes,
        "git_info": git_info,
        "system_info": sys_info,
        "package_versions": pkg_info
    }
    
    out_file = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "phase0_verification.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2)
    print(f"\nVerification results saved to {out_file}")

if __name__ == "__main__":
    main()
