# AOSP Carrier ID data

`carrier_list.textpb` is copied from Android Open Source Project's
`platform/packages/providers/TelephonyProvider` repository and is licensed under
Apache-2.0. It is used offline to identify a SIM's home network and, when SPN/GID/IMSI
rules match, its specific MVNO brand.

Vendored revision: `bca387f553a4493c88e24455172225fd1049c91f`

Refresh deliberately, after reviewing the upstream diff:

```bash
python3 tools/update_aosp_carrier_data.py <full-reviewed-commit>
```

The visited-network display aliases in `carrier_id.visited_network` deliberately cover only
the exact, reviewed PLMNs 46000 (China Mobile), 46001 (China Unicom), and 46011 (China Telecom).
Tests verify them against this vendored table's unconditional network records. They do not
change SIM/MVNO resolution, infer a carrier from a stale reported name, or normalize MNC length.
The 46000/46001 assignments are also listed in the
[ITU E.212 bulletin](https://www.itu.int/dms_pub/itu-t/opb/sp/T-SP-E.212B-2023-PDF-E.pdf).
