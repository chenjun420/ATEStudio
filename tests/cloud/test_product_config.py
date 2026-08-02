"""Tests for product config CRUD API endpoints.

Tests cover:
- POST /api/v1/products — create returns 201, duplicate product_type returns 409
- GET /api/v1/products — list returns 200 with items and total
- GET /api/v1/products/{product_type} — get returns 200, nonexistent returns 404
- PUT /api/v1/products/{product_type} — update modifies fields, 404/409 handling
- DELETE /api/v1/products/{product_type} — delete returns 204, 404 for nonexistent
- Schema validation — invalid data returns 422
- DB persistence — data is actually stored and retrievable
- Shared model YAML round-trip serialization
"""

import pytest
from pydantic import ValidationError

from shared.product_config import (
    ProductConfig,
    ProductConfigList,
    parse_product_config,
    parse_product_config_list,
    serialize_product_config,
    serialize_product_config_list,
)


def _sample_config_data(product_type: str = "comm_module_v2") -> dict[str, object]:
    """Return a sample product config dict for testing."""
    return {
        "product_type": product_type,
        "test_sequence_ref": "seq_final_test_v1",
        "test_limits": ["limits_v2.1.0", "limits_rf_v1.0"],
        "instrument_assignments": {"dmm": "DMM-01", "osc": "OSC-01"},
        "checkpoints": ["cp_power_on", "cp_rf_calibration", "cp_final_check"],
    }


class TestCreateProductConfig:
    """Tests for POST /api/v1/products endpoint."""

    @pytest.mark.asyncio
    async def test_create_product_config(self, client):
        """Test creating a new product config returns 201."""
        response = await client.post("/api/v1/products", json=_sample_config_data())

        assert response.status_code == 201
        data = response.json()
        assert data["product_type"] == "comm_module_v2"
        assert data["test_sequence_ref"] == "seq_final_test_v1"
        assert data["test_limits"] == ["limits_v2.1.0", "limits_rf_v1.0"]
        assert data["instrument_assignments"] == {"dmm": "DMM-01", "osc": "OSC-01"}
        assert data["checkpoints"] == ["cp_power_on", "cp_rf_calibration", "cp_final_check"]
        assert "id" in data
        assert "created_at" in data
        assert "updated_at" in data

    @pytest.mark.asyncio
    async def test_create_product_config_minimal(self, client):
        """Test creating a config with only required fields."""
        config_data = {
            "product_type": "simple_board_v1",
            "test_sequence_ref": "seq_basic",
        }
        response = await client.post("/api/v1/products", json=config_data)

        assert response.status_code == 201
        data = response.json()
        assert data["product_type"] == "simple_board_v1"
        assert data["test_sequence_ref"] == "seq_basic"
        assert data["test_limits"] == []
        assert data["instrument_assignments"] == {}
        assert data["checkpoints"] == []

    @pytest.mark.asyncio
    async def test_create_product_config_invalid(self, client):
        """Test creating a config with invalid data returns 422."""
        config_data = {
            "product_type": "",  # Empty is invalid
            "test_sequence_ref": "seq_test",
        }
        response = await client.post("/api/v1/products", json=config_data)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_product_config_missing_required(self, client):
        """Test creating a config without required fields returns 422."""
        config_data = {
            "product_type": "missing_ref",
            # test_sequence_ref is missing
        }
        response = await client.post("/api/v1/products", json=config_data)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_product_config_duplicate(self, client):
        """Test creating a config with duplicate product_type returns 409."""
        await client.post("/api/v1/products", json=_sample_config_data())

        response = await client.post("/api/v1/products", json=_sample_config_data())
        assert response.status_code == 409
        assert "already exists" in response.json()["detail"].lower()


class TestListProductConfigs:
    """Tests for GET /api/v1/products endpoint."""

    @pytest.mark.asyncio
    async def test_list_empty(self, client):
        """Test listing when no configs exist."""
        response = await client.get("/api/v1/products")

        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_with_data(self, client):
        """Test listing returns all created configs."""
        await client.post("/api/v1/products", json=_sample_config_data("product_a"))
        await client.post("/api/v1/products", json=_sample_config_data("product_b"))
        await client.post("/api/v1/products", json=_sample_config_data("product_c"))

        response = await client.get("/api/v1/products")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3
        product_types = {item["product_type"] for item in data["items"]}
        assert product_types == {"product_a", "product_b", "product_c"}


class TestGetProductConfig:
    """Tests for GET /api/v1/products/{product_type} endpoint."""

    @pytest.mark.asyncio
    async def test_get_product_config(self, client):
        """Test getting a config by product_type returns 200."""
        await client.post("/api/v1/products", json=_sample_config_data())

        response = await client.get("/api/v1/products/comm_module_v2")

        assert response.status_code == 200
        data = response.json()
        assert data["product_type"] == "comm_module_v2"
        assert data["test_sequence_ref"] == "seq_final_test_v1"
        assert data["test_limits"] == ["limits_v2.1.0", "limits_rf_v1.0"]
        assert data["instrument_assignments"] == {"dmm": "DMM-01", "osc": "OSC-01"}
        assert data["checkpoints"] == ["cp_power_on", "cp_rf_calibration", "cp_final_check"]

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, client):
        """Test getting a nonexistent config returns 404."""
        response = await client.get("/api/v1/products/nonexistent_product")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


class TestUpdateProductConfig:
    """Tests for PUT /api/v1/products/{product_type} endpoint."""

    @pytest.mark.asyncio
    async def test_update_fields(self, client):
        """Test updating fields on an existing config."""
        await client.post("/api/v1/products", json=_sample_config_data())

        update_data = {
            "test_sequence_ref": "seq_updated_v2",
            "test_limits": ["limits_v3.0.0"],
            "checkpoints": ["cp_new_checkpoint"],
        }
        response = await client.put("/api/v1/products/comm_module_v2", json=update_data)

        assert response.status_code == 200
        data = response.json()
        assert data["test_sequence_ref"] == "seq_updated_v2"
        assert data["test_limits"] == ["limits_v3.0.0"]
        assert data["checkpoints"] == ["cp_new_checkpoint"]
        # Unchanged fields remain
        assert data["instrument_assignments"] == {"dmm": "DMM-01", "osc": "OSC-01"}
        assert data["product_type"] == "comm_module_v2"

    @pytest.mark.asyncio
    async def test_update_product_type_rename(self, client):
        """Test renaming the product_type via update."""
        await client.post("/api/v1/products", json=_sample_config_data("old_type"))

        response = await client.put(
            "/api/v1/products/old_type",
            json={"product_type": "new_type"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["product_type"] == "new_type"

        # Old product_type no longer exists
        get_old = await client.get("/api/v1/products/old_type")
        assert get_old.status_code == 404

        # New product_type is retrievable
        get_new = await client.get("/api/v1/products/new_type")
        assert get_new.status_code == 200

    @pytest.mark.asyncio
    async def test_update_nonexistent(self, client):
        """Test updating a nonexistent config returns 404."""
        response = await client.put(
            "/api/v1/products/nonexistent",
            json={"test_sequence_ref": "seq_new"},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_update_duplicate_product_type(self, client):
        """Test renaming to an existing product_type returns 409."""
        await client.post("/api/v1/products", json=_sample_config_data("type_a"))
        await client.post("/api/v1/products", json=_sample_config_data("type_b"))

        response = await client.put(
            "/api/v1/products/type_b",
            json={"product_type": "type_a"},
        )
        assert response.status_code == 409


class TestDeleteProductConfig:
    """Tests for DELETE /api/v1/products/{product_type} endpoint."""

    @pytest.mark.asyncio
    async def test_delete_product_config(self, client):
        """Test deleting an existing config returns 204."""
        await client.post("/api/v1/products", json=_sample_config_data())

        response = await client.delete("/api/v1/products/comm_module_v2")
        assert response.status_code == 204

        # Verify it's gone
        get_response = await client.get("/api/v1/products/comm_module_v2")
        assert get_response.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, client):
        """Test deleting a nonexistent config returns 404."""
        response = await client.delete("/api/v1/products/nonexistent")
        assert response.status_code == 404


class TestDBPersistence:
    """Tests verifying data is actually persisted to the database."""

    @pytest.mark.asyncio
    async def test_create_then_list_persists(self, client):
        """Test that created config appears in list (verifies DB persistence)."""
        await client.post("/api/v1/products", json=_sample_config_data("persisted_type"))

        list_response = await client.get("/api/v1/products")
        assert list_response.status_code == 200
        data = list_response.json()
        assert data["total"] == 1
        assert data["items"][0]["product_type"] == "persisted_type"

    @pytest.mark.asyncio
    async def test_update_persists(self, client):
        """Test that updates are persisted across requests."""
        await client.post("/api/v1/products", json=_sample_config_data("persist_update"))

        await client.put(
            "/api/v1/products/persist_update",
            json={"test_sequence_ref": "seq_persisted_v2"},
        )

        # Fetch in a new request to verify persistence
        get_response = await client.get("/api/v1/products/persist_update")
        assert get_response.status_code == 200
        assert get_response.json()["test_sequence_ref"] == "seq_persisted_v2"

    @pytest.mark.asyncio
    async def test_delete_persists(self, client):
        """Test that deletion is persisted across requests."""
        await client.post("/api/v1/products", json=_sample_config_data("persist_delete"))

        await client.delete("/api/v1/products/persist_delete")

        # Verify in a new request
        list_response = await client.get("/api/v1/products")
        assert list_response.json()["total"] == 0


class TestSharedProductConfigModel:
    """Tests for the shared Pydantic model and YAML serialization."""

    def test_product_config_creation(self):
        """Test ProductConfig model creation with all fields."""
        config = ProductConfig(
            product_type="test_product",
            test_sequence_ref="seq_001",
            test_limits=["limit_1", "limit_2"],
            instrument_assignments={"dmm": "DMM-01"},
            checkpoints=["cp_1", "cp_2"],
        )
        assert config.product_type == "test_product"
        assert config.test_sequence_ref == "seq_001"
        assert config.test_limits == ["limit_1", "limit_2"]
        assert config.instrument_assignments == {"dmm": "DMM-01"}
        assert config.checkpoints == ["cp_1", "cp_2"]

    def test_product_config_defaults(self):
        """Test ProductConfig defaults for optional collections."""
        config = ProductConfig(
            product_type="minimal",
            test_sequence_ref="seq_min",
        )
        assert config.test_limits == []
        assert config.instrument_assignments == {}
        assert config.checkpoints == []

    def test_product_config_extra_forbid(self):
        """Test that extra fields are forbidden."""
        with pytest.raises(ValidationError):
            ProductConfig(
                product_type="test",
                test_sequence_ref="seq",
                unknown_field="should_fail",
            )

    def test_product_config_list(self):
        """Test ProductConfigList wrapping multiple configs."""
        config_list = ProductConfigList(
            configs=[
                ProductConfig(product_type="a", test_sequence_ref="seq_a"),
                ProductConfig(product_type="b", test_sequence_ref="seq_b"),
            ]
        )
        assert len(config_list.configs) == 2
        assert config_list.configs[0].product_type == "a"
        assert config_list.configs[1].product_type == "b"

    def test_yaml_round_trip(self):
        """Test YAML serialization and deserialization round-trip."""
        original = ProductConfig(
            product_type="round_trip_product",
            test_sequence_ref="seq_rt",
            test_limits=["limit_a", "limit_b"],
            instrument_assignments={"dmm": "DMM-01", "osc": "OSC-01"},
            checkpoints=["cp_start", "cp_end"],
        )
        yaml_str = serialize_product_config(original)
        restored = parse_product_config(yaml_str)

        assert restored.product_type == original.product_type
        assert restored.test_sequence_ref == original.test_sequence_ref
        assert restored.test_limits == original.test_limits
        assert restored.instrument_assignments == original.instrument_assignments
        assert restored.checkpoints == original.checkpoints

    def test_yaml_round_trip_list(self):
        """Test YAML list serialization and deserialization round-trip."""
        original = ProductConfigList(
            configs=[
                ProductConfig(product_type="a", test_sequence_ref="seq_a"),
                ProductConfig(product_type="b", test_sequence_ref="seq_b"),
            ]
        )
        yaml_str = serialize_product_config_list(original)
        restored = parse_product_config_list(yaml_str)

        assert len(restored.configs) == 2
        assert restored.configs[0].product_type == "a"
        assert restored.configs[1].product_type == "b"
