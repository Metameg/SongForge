# Archived dashboards

These four dashboards were consolidated into the single
`../dashboards/songforge-overview.json` page. They live here (outside the
Grafana-provisioned `dashboards/` mount) so Grafana loads only the one overview
page, while the original issue-#18 "capacity thesis" breakdowns are preserved.

To bring one back into Grafana, move it into `../dashboards/` — provisioning
picks it up within ~10s.

- `capacity-thesis.json` — listeners-rise-while-datastores-stay-flat proof
- `connection-saturation.json` — layer-by-layer connection/pool walls
- `pipeline.json` — jobs-by-state, generation semaphore, failure/refund rates
- `service-red.json` — full RED + USE reference
