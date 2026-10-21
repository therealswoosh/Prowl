<#
.SYNOPSIS
  Capture SteelSeries GG USB traffic to the MSI keyboard with USBPcap + tshark.

.DESCRIPTION
  Run Wireshark's tshark with a USBPcap interface. Install Wireshark with the
  USBPcap component first, and run this from an elevated PowerShell.

.EXAMPLE
  .\capture-windows.ps1 -List
  .\capture-windows.ps1 -Interface 4 -Seconds 90 -Output gg-red.pcapng
  tshark -r gg-red.pcapng -Y "usb.setup.bRequest == 0x09" -T fields `
    -E separator=/t -e frame.number -e usb.device_address -e usb.setup.wValue `
    -e usb.setup.wIndex -e usb.capdata -e usb.data_fragment > red.tsv
#>
param(
    [switch]$List,
    [int]$Interface = 0,
    [int]$Seconds = 60,
    [string]$Output = "gg-capture.pcapng"
)

$tshark = Get-Command tshark -ErrorAction SilentlyContinue
if (-not $tshark) {
    Write-Error "tshark not found. Install Wireshark with the USBPcap component."
    exit 1
}

if ($List) {
    & tshark -D
    exit 0
}

Write-Host "Capturing $Seconds s on interface $Interface -> $Output"
Write-Host "Now change ONE key to a distinctive colour in SteelSeries GG and apply it."
& tshark -i $Interface -w $Output -a "duration:$Seconds"
Write-Host "Done. Decode it with: python -m msikb decode $Output"
