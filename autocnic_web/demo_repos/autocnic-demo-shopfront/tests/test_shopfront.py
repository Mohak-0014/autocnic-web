from shopfront.pricing import compute_subtotal, compute_tier_discount
from shopfront.shipping import compute_shipping_cost
from shopfront.fraud import compute_fraud_score
from shopfront.inventory import compute_backorder_fee
from shopfront.checkout import compute_order_total


def test_subtotal():
    assert compute_subtotal(10.0, 3) == 30.0


def test_standard_tier_gets_no_discount():
    assert compute_tier_discount("standard", 0.3, 100.0) == 0.0
    assert compute_tier_discount("gold", 0.1, 100.0) == 10.0
    assert compute_tier_discount("platinum", 0.1, 100.0) == 15.0


def test_free_shipping_over_threshold():
    assert compute_shipping_cost(250.0, 999.0, "express") == 0.0
    assert compute_shipping_cost(50.0, 100.0, "standard") == 9.5


def test_fraud_and_backorder():
    assert compute_fraud_score(100.0, 1.0) == 0.05
    assert compute_fraud_score(300.0, 1.0) == 1.0
    assert compute_backorder_fee(5, 3) == 0.0
    assert compute_backorder_fee(1, 3) == 5.0


def test_total_adds_hold_fee():
    assert compute_order_total(100.0, 0.0, 8.0, 0.0, 0.0, 0.9) == 113.0
    assert compute_order_total(100.0, 0.0, 8.0, 0.0, 0.0, 0.1) == 108.0
