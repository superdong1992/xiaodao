# Generic locator output contract

Write `output/generic_diagnosis_result.txt` as strict UTF-8, at most 65536 bytes, with no code fence or text outside this exact envelope:

```text
<<<GENERIC_DIAGNOSIS_RESULT_V1>>>
STATUS: RESOLVED
CONCLUSION:
non-empty diagnosis conclusion
ROOT_CAUSE_ANALYSIS:
non-empty textual root cause analysis
<<<END_GENERIC_DIAGNOSIS_RESULT_V1>>>
```

`STATUS` is exactly `RESOLVED` or `UNRESOLVED`. For `UNRESOLVED`, state the likely causes and the missing information that prevents confirmation.
