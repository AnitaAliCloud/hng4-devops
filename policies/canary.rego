package canary

import rego.v1

default allow := false

allow if {
    count(violations) == 0
}

violations contains msg if {
    input.error_rate > data.canary.max_error_rate
    msg := sprintf(
        "Error rate (%.2f%%) exceeds maximum threshold (%.2f%%)",
        [input.error_rate * 100, data.canary.max_error_rate * 100]
    )
}

violations contains msg if {
    input.p99_latency_ms > data.canary.max_p99_latency_ms
    msg := sprintf(
        "P99 latency (%dms) exceeds maximum threshold (%dms)",
        [input.p99_latency_ms, data.canary.max_p99_latency_ms]
    )
}