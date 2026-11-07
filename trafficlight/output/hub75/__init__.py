"""Host side of the custom HUB75 firmware for the Colorlight 5A-75B (docs/FIRMWARE.md).

The card is a sequential streamer: it reads bit planes from SDRAM in the order a host-built
schedule table says and pulses OE for the time the table says. Everything clever is here:

* ``schedule``: the visit table (which plane, how long) and its budget.
* ``pack``: card image -> frame-buffer bytes (gamma, temporal dithering, bit planes).
* ``protocol``: the UDP packets.
* ``model``: a behavioural model of the card (receiver state machine and streamer), for tests
  and as the reference for gateware tests.
"""
