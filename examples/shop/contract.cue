// The shop contract, written by hand: the same contract as contract.json, which app.py
// loads. docuconf docs reads this file; CI checks that the two agree.
package shop

import "docuconf.dev/contract"

contract.#Contract & {
	apiVersion: "docuconf.dev/v1alpha1"
	kind:       "ConfigContract"
	metadata: {
		name: "shop"
		generator: {
			language: "manual"
			sdk:      "hand-written"
			version:  "1.0.0"
		}
	}
	includes: [
		"otel@1"
	]
	roles: [
		"web",
		"worker",
	]
	vars: {
		DOCUCONF_ROLE: {
			type:        "enum"
			description: "The role this workload runs: web or worker"
			required:    true
			values: [
				"web",
				"worker",
			]
		}
		DATABASE_URL: {
			type:        "url"
			description: "Postgres connection string"
			required:    true
			secret:      true
			schemes: [
				"postgres",
				"postgresql",
			]
		}
		PORT: {
			type:        "int"
			description: "Port the web server listens on"
			default:     8080
			min:         1
			max:         65535
			roles: [
				"web"
			]
		}
		ALLOWED_ORIGINS: {
			type:        "list"
			description: "Origins the web server accepts browser requests from"
			items:       "string"
			required:    true
			roles: [
				"web"
			]
		}
		QUEUES: {
			type:        "list"
			description: "Queues the worker takes jobs from, in priority order"
			items:       "string"
			required:    true
			roles: [
				"worker"
			]
		}
		LOG_FORMAT: {
			type:        "enum"
			description: "Log output format"
			values: [
				"text",
				"json",
			]
			default: "text"
		}
		LOG_SCHEMA_URL: {
			type:        "url"
			description: "JSON schema the structured logs follow"
			schemes: [
				"https"
			]
			requiredIf: LOG_FORMAT: "json"
		}
		WEBHOOKS_ENABLED: {
			type:        "bool"
			description: "Send order webhooks to partners"
			default:     false
			roles: [
				"worker"
			]
		}
		OTEL_SERVICE_NAME: {
			description: "Service name reported in telemetry"
			required:    false
			secret:      false
			type:        "string"
		}
		OTEL_RESOURCE_ATTRIBUTES: {
			description: "Resource attributes, comma-separated key=value pairs"
			required:    false
			secret:      false
			type:        "string"
		}
		OTEL_EXPORTER_OTLP_ENDPOINT: {
			description: "Base URL of the OTLP collector"
			required:    false
			secret:      false
			type:        "url"
			schemes: [
				"http",
				"https",
			]
		}
		OTEL_EXPORTER_OTLP_PROTOCOL: {
			description: "Transport protocol for OTLP export"
			required:    false
			secret:      false
			type:        "enum"
			values: ["grpc", "http/protobuf", "http/json"]
		}
		OTEL_EXPORTER_OTLP_HEADERS: {
			description: "Headers sent with every OTLP export, comma-separated key=value pairs"
			required:    false
			secret:      true
			type:        "string"
		}
		OTEL_TRACES_SAMPLER: {
			description: "Sampler for traces"
			required:    false
			secret:      false
			type:        "enum"
			values: ["always_on", "always_off", "traceidratio", "parentbased_always_on", "parentbased_always_off", "parentbased_traceidratio", "jaeger_remote", "parentbased_jaeger_remote", "xray"]
		}
		OTEL_TRACES_SAMPLER_ARG: {
			description: "Argument for the sampler, such as the ratio for traceidratio"
			required:    false
			secret:      false
			type:        "string"
		}
		OTEL_TRACES_EXPORTER: {
			description: "Trace exporters, comma-separated, such as otlp or none"
			required:    false
			secret:      false
			type:        "string"
		}
		OTEL_METRICS_EXPORTER: {
			description: "Metric exporters, comma-separated, such as otlp or none"
			required:    false
			secret:      false
			type:        "string"
		}
		OTEL_LOGS_EXPORTER: {
			description: "Log exporters, comma-separated, such as otlp or none"
			required:    false
			secret:      false
			type:        "string"
		}
		OTEL_PROPAGATORS: {
			description: "Context propagators, comma-separated, such as tracecontext,baggage"
			required:    false
			secret:      false
			type:        "string"
		}
		OTEL_SDK_DISABLED: {
			description: "Disable the OpenTelemetry SDK entirely"
			required:    false
			secret:      false
			type:        "bool"
		}
	}
	files: "webhook-signing-key": {
		type:        "text"
		description: "Key that signs outgoing order webhooks"
		secret:      true
		path:        "/etc/shop/webhooks/signing.key"
		minLength:   32
		requiredIf: WEBHOOKS_ENABLED: true
		roles: [
			"worker"
		]
	}
}
